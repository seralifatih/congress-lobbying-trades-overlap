# CLAUDE.md

Project guidance for Claude Code. Read this before touching any file.

## What this is

Apify actor that joins **US federal lobbying disclosures (LDA)** with **Congressional stock trading disclosures (STOCK Act / PTR filings)** and surfaces overlaps: a member trades in sector X during the same quarter that sector X lobbies a committee the member sits on.

This is the third actor in a set. The other two already have paying users:
- `seralifatih/congress-house-trades`
- `seralifatih/congress-senate-trades`

Design decisions should assume the buyer of those two actors is the buyer of this one. Cross-sell is the whole point; a standalone lobbying scraper is not the product.

## Non-negotiable framing

**This is a records product, not a signal product.** We publish what the filings say. We do not claim causation, do not score "corruption", do not emit trade recommendations. Every output row must be traceable to a specific filing ID and source URL.

Rationale: a previous actor (`pm-arbitrage`) failed because it sold a perishable signal. Records with archive value sell; signals don't. If a feature makes the output feel like alpha rather than evidence, cut it.

**Overlap ≠ wrongdoing.** Language in README, field names, and log lines stays neutral: `overlap`, `co_occurrence`, `same_quarter_activity`. Never `suspicious`, `insider`, `corrupt`, `conflict_of_interest` as a factual assertion. This is both an accuracy issue and a legal-exposure issue — these are named living people.

## Data sources

| Source | Access | Cadence | Notes |
|---|---|---|---|
| Senate LDA REST API | `lda.gov/api/` — free, needs registered API key | Quarterly | Primary lobbying source. Rate-limited; respect it. |
| House Clerk LD-1/LD-2 | XML bulk download | Quarterly | Overlaps heavily with Senate LDA. Use as fill-in, not primary. |
| House PTR filings | Existing actor's pipeline | Ad hoc (45-day window) | Reuse, do not rewrite. |
| Senate PTR filings | Existing actor's pipeline | Ad hoc | Reuse, do not rewrite. |
| Committee membership | Congress.gov API or unitedstates/congress-legislators repo | Per-Congress | Needed for the join. Cache aggressively; changes rarely. |

Do **not** scrape OpenSecrets. Their terms prohibit it and their data is derivative anyway.

## The join — where the real work is

The hard part is not fetching. It is entity resolution across three vocabularies that do not share keys:

1. **Lobbying issue codes** (LDA general issue areas: `TAX`, `HCR`, `DEF`, ~80 codes) → sector
2. **Traded tickers** → sector (GICS or SIC)
3. **Committee jurisdiction** → sector

There is no official crosswalk between any pair of these. We build and version our own mapping table, ship it as a data file in the repo, and expose it in the output so users can audit it.

**Requirements for the crosswalk:**
- Lives in `data/crosswalk.yaml`, human-readable, hand-editable
- Every mapping row carries a `confidence` field (`high` / `medium` / `low`)
- Output rows expose which mapping rule fired (`mapping_rule_id`)
- Unmapped tickers/codes are reported in the run summary, never silently dropped

If the crosswalk is wrong the whole product is wrong. Treat it as the core asset, not glue code.

## Output contract

Each record = one (member, quarter, sector) overlap. Not one per trade, not one per filing.

Required fields on every record:
- `member_bioguide_id`, `member_name`, `chamber`, `party`, `state`
- `quarter` (e.g. `2026-Q1`)
- `sector`, `mapping_rule_id`, `mapping_confidence`
- `trades[]` — each with `ptr_filing_id`, `ptr_url`, `ticker`, `transaction_type`, `amount_range`, `transaction_date`, `disclosure_date`
- `lobbying[]` — each with `lda_filing_uuid`, `lda_url`, `registrant`, `client`, `issue_codes[]`, `amount_reported`
- `committees[]` — member's committee assignments that quarter, with jurisdiction tags
- `overlap_type` — enum: `committee_match` (lobbying targeted a committee they sit on) / `sector_match_only` (sector overlap, no committee link)
- `disclosure_lag_days` — trade date to disclosure date. This is a genuinely interesting derived field and it is factual, not interpretive.

Also write a `RUN_SUMMARY` to the key-value store: quarters covered, members scanned, overlaps found by type, unmapped codes, source freshness timestamps.

## Pricing

Per-run, not per-result. `pm-arbitrage` priced per-result at $250/1k and got 4 monthly actives; users could not predict their bill and a run returning zero overlaps still has informational value.

Target: flat per-run, in line with the existing Congress actors. Match their model exactly — same buyer, same expectations.

## Tech

- Python 3.11, async-first
- `httpx` with bounded concurrency
- `pydantic` v2 for the output schema — strict, no `Any`
- Apify SDK for Python
- Cache committee membership and the legislator roster to the key-value store; they change per-Congress, not per-run

## Style

- Type hints everywhere
- No bare `except`
- Every external call wrapped with retry + explicit timeout
- Log at INFO what quarter/source is being fetched; DEBUG for per-filing detail
- Tests for the crosswalk logic specifically — that is where bugs are expensive

## What not to build

- No alerting, no webhooks, no "watchlist" features in v1
- No sentiment or scoring model
- No UI
- No attempt to cover state-level lobbying (different sources per state, enormous scope creep)
- No real-time anything — this data is quarterly by law

## Open questions to resolve before v2

- Whether committee jurisdiction tagging is reliable enough to make `committee_match` the headline, or whether it needs manual curation per committee
- How far back to backfill; LDA API has usable history to 2016
- Whether spouse/dependent trades (disclosed but attributed differently) should be a separate field or merged
