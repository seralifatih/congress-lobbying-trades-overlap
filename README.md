# Congress Lobbying × Trades Overlap

**Cross-reference US federal lobbying disclosures with Congressional stock trading disclosures — one auditable record per overlap.**

This actor joins two public disclosure systems that don't share keys: quarterly lobbying filings under the Lobbying Disclosure Act (LDA) and member stock transactions disclosed under the STOCK Act (PTR filings). It surfaces **same-quarter co-occurrence**: a member traded in sector X during a quarter in which sector X was the subject of lobbying activity — and, where the member sits on a committee with jurisdiction over that sector, it says so.

Every output row is traceable to specific filing IDs and source URLs. This is a records product: it reports what the filings say, in a form you can archive, query, and verify. It does not score, rank, or interpret.

> **An overlap is not a finding of wrongdoing.** Members of Congress trade securities and industries lobby Congress; both are legal, disclosed, and continuous. Co-occurrence within a quarter is a factual observation about two public datasets, nothing more. This actor makes no claim of causation and emits no trade recommendations.

Works as a companion to the [House Trading Pipeline](https://apify.com/seralifatih/congress-trading-pipeline-1) and the [Senate Trading Pipeline](https://apify.com/seralifatih/congress-trading-pipeline) — it reads their trade data directly, so records stay consistent across all three.

## What you get

One record per **(member, quarter, sector)** overlap — not one per trade, not one per filing. Each record bundles the full evidence on both sides:

```json
{
  "member_bioguide_id": "A000379",
  "member_name": "Mark Alford",
  "chamber": "house",
  "party": "R",
  "state": "MO",
  "quarter": "2026-Q1",
  "sector": "defense",
  "mapping_rule_id": "tk:LMT->defense",
  "mapping_confidence": "high",
  "overlap_type": "committee_match",
  "disclosure_lag_days": 30,
  "trades": [
    {
      "ptr_filing_id": "4d6016b4...",
      "ptr_url": "https://disclosures-clerk.house.gov/...",
      "ticker": "LMT",
      "transaction_type": "purchase",
      "amount_range": "$1,001 - $15,000",
      "transaction_date": "2026-01-05",
      "disclosure_date": "2026-02-04"
    }
  ],
  "lobbying": [
    {
      "lda_filing_uuid": "7866327b-c892-4430-b9f0-1f0f679c58c6",
      "lda_url": "https://lda.gov/api/v1/filings/7866327b-.../",
      "registrant": "Example Government Affairs LLC",
      "client": "Example Defense Corp",
      "issue_codes": ["DEF", "BUD"],
      "amount_reported": 240000.0
    }
  ],
  "lobbying_filing_count": 38,
  "committees": [
    {
      "committee_id": "HSAS",
      "committee_name": "House Committee on Armed Services",
      "jurisdiction_tags": ["defense", "aerospace"],
      "is_subcommittee": false,
      "role": "Member"
    }
  ]
}
```

### `overlap_type`

- **`committee_match`** — the member sits on at least one committee whose jurisdiction covers the overlap sector. The matched committee assignments are included as evidence.
- **`sector_match_only`** — the sector overlap exists, but no committee link does.

LDA filings disclose which chamber or agency was lobbied, not which committee — so committee matching is resolved through sector jurisdiction, and the record shows exactly which committee and which jurisdiction tag produced the match.

### `lobbying_filing_count`

Popular sectors can attract over a thousand filings in a quarter, so the `lobbying[]` evidence list is capped (default 100, configurable) to the filings with the largest reported amounts. `lobbying_filing_count` always carries the uncapped total — truncation is visible on the record, never silent.

### `disclosure_lag_days`

Days between a trade's transaction date and its public disclosure date (earliest trade in the record). A factual, derived field — the STOCK Act allows up to 45 days.

## The crosswalk is yours to audit

There is no official mapping between LDA issue codes, stock tickers, and committee jurisdictions. Every product in this space invents one — most keep it hidden. **This one ships in the open, in the repo, as hand-editable YAML:**

- [`data/crosswalk.yaml`](data/crosswalk.yaml) — all 79 official LDA general issue codes → sectors, plus ticker → sector rules (explicit per-ticker overrides and a GICS-sector fallback)
- [`data/committee_jurisdictions.yaml`](data/committee_jurisdictions.yaml) — every current House, Senate, and joint committee → jurisdiction sectors

Every mapping row carries a `confidence` grade (`high` / `medium` / `low`), and **every output record names the exact rule that fired** (`mapping_rule_id`, e.g. `ic:HCR->healthcare`, `tk:LMT->defense`, `com:HSAS->defense`). If you disagree with a mapping, you can see it, trace it, and change it — the run summary also lists every issue code, ticker, and committee the crosswalk could *not* resolve, so nothing is silently dropped.

If you build on this data, you know exactly what joined to what and why. That is the point.

## Data sources

| Source | What it provides |
|---|---|
| Senate LDA REST API (`lda.gov`) | Quarterly lobbying filings: registrant, client, issue codes, reported amounts |
| [House](https://apify.com/seralifatih/congress-trading-pipeline-1) / [Senate](https://apify.com/seralifatih/congress-trading-pipeline) trading pipeline actors | Member stock transactions from PTR filings |
| `unitedstates/congress-legislators` | Member roster and committee membership (cached, refreshed monthly) |

All sources are official or community-maintained public records. No scraping of third-party aggregators.

## Input

| Field | Type | Default | Description |
|---|---|---|---|
| `quarters` | array | last completed quarter | Quarters to cover, e.g. `["2026-Q1"]` |
| `chambers` | array | `["house", "senate"]` | Which chambers to scan |
| `overlap_types` | array | both | Filter to `committee_match` and/or `sector_match_only` |
| `lda_api_key` | secret | — | Optional. The actor ships with a shared key sufficient for typical runs. Provide your own free key from lda.gov for heavy multi-quarter backfills or guaranteed throughput. |
| `max_concurrency` | integer | `5` | Outbound API concurrency |
| `lda_max_pages` | integer | — | Debug cap for cheap test runs |

## Run summary

Every run — including runs that find zero overlaps — writes a `RUN_SUMMARY` to the key-value store: quarters covered, members scanned, overlap counts by type, every unmapped code/ticker/committee/name, and source freshness timestamps. A quarter with no overlaps is itself a fact worth recording.

## How to use

**Apify Console (no code):** open the actor, pick your quarters, run. Results land in the dataset; export as JSON, CSV, or Excel.

**API:**

```bash
curl -X POST "https://api.apify.com/v2/acts/seralifatih~congress-lobbying-trades-overlap/runs?token=$APIFY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"quarters": ["2026-Q1"], "chambers": ["house", "senate"]}'
```

**Scheduled:** lobbying data is quarterly by law. A quarterly schedule a few weeks after each LDA filing deadline (Jan 20, Apr 20, Jul 20, Oct 20) keeps a complete archive with four runs a year.

## Pricing

**Predictable per-run pricing.** A small flat start fee plus a fixed charge per quarter processed — you know the exact cost before you run, because you choose the quarters in the input. One quarter or eight, the bill follows your selection, not the number of overlaps found. A run that returns zero overlaps still delivers the run summary and the negative result, which for archival use is an answer, not a failure.

## Tech

- Python 3.11, fully async (`httpx`, bounded concurrency)
- Strict `pydantic` v2 output schema — every record validated before it reaches the dataset
- Retry with exponential backoff and explicit timeouts on every external call
- Committee membership and member roster cached between runs (they change per-Congress, not per-run)

## Limitations

- Federal only. State-level lobbying is out of scope.
- Quarterly granularity — that is the resolution the LDA imposes; nothing here is or can be real-time.
- Sector mapping is inherently judgment-laden. The crosswalk exposes every judgment it makes (`mapping_rule_id`, `confidence`) so you can audit or override them, but no mapping of tickers and issue codes to sectors is beyond argument.
- Trades without a listed ticker (real estate, private funds, bonds) are excluded and counted in the run summary.

## Disclaimer

This actor republishes and cross-references public disclosure records. An overlap record documents that two disclosed activities occurred in the same quarter and sector — it is not evidence of impropriety by any person, and must not be presented as such. Nothing in this actor's output is investment, legal, or any other kind of advice.