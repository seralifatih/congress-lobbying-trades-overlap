"""Senate LDA REST API source.

Fetches quarterly LD-2 filings from lda.senate.gov and maps raw responses
to the `LobbyingFiling` model. This is the primary lobbying source
(House Clerk XML is fill-in only — see CLAUDE.md).

Design constraints enforced here:
- Bounded concurrency via an asyncio.Semaphore.
- Every request has an explicit timeout.
- Retry with exponential backoff; 429 honours `Retry-After` when present.
- API key read from env, sent as `Authorization: Token <key>`.

Standalone use — fetch one quarter to JSON for manual inspection:

    LDA_API_KEY=xxx python -m src.sources.lda 2026-Q1 --out q.json

Runs without a key too (LDA allows anonymous access at a lower rate limit),
but a key is strongly recommended to avoid throttling.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import re
import sys
from collections.abc import AsyncIterator

import httpx

# Support both `python -m src.sources.lda` (package) and direct execution.
if __package__:
    from ..models import LobbyingFiling
else:  # pragma: no cover - only when run as a loose script
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from models import LobbyingFiling  # type: ignore[no-redefine]

logger = logging.getLogger(__name__)

BASE_URL = "https://lda.senate.gov/api/v1/filings/"
API_KEY_ENV = "LDA_API_KEY"

# Our quarter label (2026-Q1) -> LDA filing_period query value.
_QUARTER_TO_PERIOD: dict[str, str] = {
    "Q1": "first_quarter",
    "Q2": "second_quarter",
    "Q3": "third_quarter",
    "Q4": "fourth_quarter",
}
_QUARTER_RE = re.compile(r"^(?P<year>\d{4})-(?P<q>Q[1-4])$")

DEFAULT_TIMEOUT = 30.0  # seconds; LDA can be slow on large pages
DEFAULT_PAGE_SIZE = 100  # LDA max is 100
DEFAULT_MAX_CONCURRENCY = 5
MAX_RETRIES = 4
BACKOFF_BASE = 1.0  # seconds
BACKOFF_CAP = 30.0  # seconds


class LDAError(RuntimeError):
    """Non-retryable failure talking to the LDA API."""


def _parse_quarter(quarter: str) -> tuple[int, str]:
    """'2026-Q1' -> (2026, 'first_quarter'). Raise on malformed input."""
    match = _QUARTER_RE.match(quarter)
    if not match:
        raise LDAError(f"malformed quarter {quarter!r}; expected e.g. '2026-Q1'")
    year = int(match["year"])
    return year, _QUARTER_TO_PERIOD[match["q"]]


def _auth_headers() -> dict[str, str]:
    headers = {"Accept": "application/json"}
    key = os.environ.get(API_KEY_ENV)
    if key:
        headers["Authorization"] = f"Token {key}"
    else:
        logger.warning(
            "%s not set; using anonymous access (lower rate limit).", API_KEY_ENV
        )
    return headers


def _backoff_delay(attempt: int, retry_after: float | None) -> float:
    """Exponential backoff with full jitter; honour Retry-After if given."""
    if retry_after is not None:
        return retry_after
    raw = min(BACKOFF_CAP, BACKOFF_BASE * (2**attempt))
    return random.uniform(0, raw)


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return float(value)  # LDA sends seconds as an integer string
    except ValueError:
        logger.debug("Unparseable Retry-After header: %r", value)
        return None


async def _get_page(
    client: httpx.AsyncClient,
    params: dict[str, str | int],
    semaphore: asyncio.Semaphore,
) -> dict:
    """GET one page with bounded concurrency, retry, and backoff.

    Retries on 429 and 5xx and on transport/timeout errors. Raises
    `LDAError` on 4xx (other than 429) and after exhausting retries.
    """
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            async with semaphore:
                response = await client.get(BASE_URL, params=params)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_exc = exc
            delay = _backoff_delay(attempt, None)
            logger.warning(
                "LDA request error (%s), attempt %d/%d, retrying in %.1fs",
                type(exc).__name__,
                attempt + 1,
                MAX_RETRIES + 1,
                delay,
            )
            await asyncio.sleep(delay)
            continue

        if response.status_code == 429 or response.status_code >= 500:
            if attempt == MAX_RETRIES:
                raise LDAError(
                    f"LDA API returned {response.status_code} after "
                    f"{MAX_RETRIES + 1} attempts"
                )
            delay = _backoff_delay(attempt, _retry_after_seconds(response))
            logger.warning(
                "LDA %d on page %s, attempt %d/%d, backing off %.1fs",
                response.status_code,
                params.get("page"),
                attempt + 1,
                MAX_RETRIES + 1,
                delay,
            )
            await asyncio.sleep(delay)
            continue

        if response.status_code >= 400:
            raise LDAError(
                f"LDA API {response.status_code} for params {params}: "
                f"{response.text[:300]}"
            )

        return response.json()

    # Loop only exits here after transport/timeout retries are exhausted.
    raise LDAError(f"LDA request failed after retries: {last_exc}")


async def iter_raw_filings(
    quarter: str,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    timeout: float = DEFAULT_TIMEOUT,
    max_pages: int | None = None,
) -> AsyncIterator[dict]:
    """Yield raw filing dicts for a quarter, paging through all results.

    Page 1 is fetched first to learn `count`; remaining pages are fetched
    with bounded concurrency. `max_pages` caps the number of pages fetched
    (a full quarter is tens of thousands of filings) — used mainly for the
    standalone-inspection CLI. None means fetch everything.
    """
    year, period = _parse_quarter(quarter)
    semaphore = asyncio.Semaphore(max_concurrency)
    base_params: dict[str, str | int] = {
        "filing_year": year,
        "filing_period": period,
        "page_size": page_size,
    }

    limits = httpx.Limits(max_connections=max_concurrency)
    async with httpx.AsyncClient(
        headers=_auth_headers(),
        timeout=httpx.Timeout(timeout),
        limits=limits,
        follow_redirects=True,
    ) as client:
        logger.info("Fetching LDA filings for %s (year=%d, %s)", quarter, year, period)
        first = await _get_page(client, {**base_params, "page": 1}, semaphore)

        count = int(first.get("count", 0))
        results = first.get("results", [])
        for item in results:
            yield item

        if not results or count <= len(results):
            logger.info("LDA %s: %d filings, single page", quarter, count)
            return

        total_pages = -(-count // page_size)  # ceil
        if max_pages is not None:
            total_pages = min(total_pages, max_pages)
            logger.info(
                "LDA %s: %d filings, capping at %d pages", quarter, count, total_pages
            )
        else:
            logger.info(
                "LDA %s: %d filings across %d pages", quarter, count, total_pages
            )
        if total_pages > 20 and API_KEY_ENV not in os.environ:
            logger.warning(
                "Fetching %d pages WITHOUT an LDA API key — anonymous access "
                "is heavily rate-limited and this will very likely fail with "
                "429s. Register a free key at lda.senate.gov and set the "
                "lda_api_key input (or %s).",
                total_pages, API_KEY_ENV,
            )

        async def fetch(page: int) -> list[dict]:
            data = await _get_page(client, {**base_params, "page": page}, semaphore)
            return data.get("results", [])

        tasks = [
            asyncio.create_task(fetch(page))
            for page in range(2, total_pages + 1)
        ]
        try:
            for coro in asyncio.as_completed(tasks):
                for item in await coro:
                    yield item
        finally:
            # On any early exit (page error after retries, cancelled
            # consumer) cancel the remaining page tasks BEFORE the client
            # context closes — otherwise they fire against a closed client
            # and flood the log with unretrieved-task exceptions.
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


def _parse_amount(raw: object) -> float | None:
    """LDA income/expenses come as decimal strings or null."""
    if raw in (None, ""):
        return None
    try:
        return float(str(raw))
    except ValueError:
        logger.debug("Unparseable amount: %r", raw)
        return None


def map_filing(raw: dict) -> LobbyingFiling | None:
    """Map one raw LDA filing dict to a `LobbyingFiling`.

    Returns None (and logs) for filings with no reportable issue codes —
    LD-2s can carry zero lobbying activities and are not useful for the join.
    `amount_reported` prefers income (lobbying firms) then expenses
    (self-filers); either may be null.
    """
    registrant = raw.get("registrant") or {}
    client = raw.get("client") or {}
    activities = raw.get("lobbying_activities") or []

    issue_codes = sorted(
        {
            a["general_issue_code"]
            for a in activities
            if a.get("general_issue_code")
        }
    )
    if not issue_codes:
        logger.debug(
            "Skipping filing %s: no issue codes", raw.get("filing_uuid")
        )
        return None

    income = _parse_amount(raw.get("income"))
    expenses = _parse_amount(raw.get("expenses"))
    amount_reported = income if income is not None else expenses

    try:
        return LobbyingFiling(
            lda_filing_uuid=raw["filing_uuid"],
            lda_url=raw["url"],
            registrant=registrant.get("name") or "<unknown registrant>",
            client=client.get("name") or "<unknown client>",
            issue_codes=issue_codes,
            amount_reported=amount_reported,
        )
    except KeyError as exc:
        raise LDAError(f"LDA filing missing required field {exc}") from exc


async def fetch_quarter(
    quarter: str,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    timeout: float = DEFAULT_TIMEOUT,
    max_pages: int | None = None,
) -> list[LobbyingFiling]:
    """Fetch and map every filing for a quarter into `LobbyingFiling`s."""
    filings: list[LobbyingFiling] = []
    skipped = 0
    async for raw in iter_raw_filings(
        quarter,
        page_size=page_size,
        max_concurrency=max_concurrency,
        timeout=timeout,
        max_pages=max_pages,
    ):
        mapped = map_filing(raw)
        if mapped is None:
            skipped += 1
            continue
        filings.append(mapped)
    logger.info(
        "LDA %s: mapped %d filings, skipped %d (no issue codes)",
        quarter,
        len(filings),
        skipped,
    )
    return filings


# --------------------------------------------------------------------------
# Standalone CLI — fetch one quarter to JSON for manual inspection.
# --------------------------------------------------------------------------
async def _main_async(args: argparse.Namespace) -> int:
    filings = await fetch_quarter(
        args.quarter,
        page_size=args.page_size,
        max_concurrency=args.concurrency,
        timeout=args.timeout,
        max_pages=args.max_pages,
    )
    payload = [f.model_dump(mode="json") for f in filings]
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
        print(f"Wrote {len(payload)} filings to {args.out}", file=sys.stderr)
    else:
        json.dump(payload, sys.stdout, indent=2, ensure_ascii=False)
        print(file=sys.stdout)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch one quarter of LDA filings.")
    parser.add_argument("quarter", help="Quarter to fetch, e.g. 2026-Q1")
    parser.add_argument("--out", help="Write JSON here instead of stdout")
    parser.add_argument(
        "--page-size", type=int, default=DEFAULT_PAGE_SIZE, dest="page_size"
    )
    parser.add_argument(
        "--concurrency", type=int, default=DEFAULT_MAX_CONCURRENCY
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        dest="max_pages",
        help="Cap pages fetched (for inspection; a full quarter is huge)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="DEBUG logging"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return asyncio.run(_main_async(args))
    except LDAError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
