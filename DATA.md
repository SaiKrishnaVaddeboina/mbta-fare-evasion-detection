# Data

The pipeline operates on the MBTA mobile-ticketing telemetry hosted in the
`masabi-fraud-detection-lab-input` AWS S3 bucket. **The raw data is not
included in this repository** and is governed by a data-use agreement with
Masabi; access is granted on a per-engagement basis.

This document specifies the schema the pipeline expects, so that the code
in [`UC2_v2/`](UC2_v2/) can be reviewed and (with the synthetic-data
generator below) executed end-to-end without the production data.

## Required tables

The pipeline consumes seven CSVs placed in `UC2_v2/data/`:

| File | Size (production) | Rows (production) | Role |
|---|---:|---:|---|
| `retail_activations.csv` | 5.2 GB | 6,407,862 | Primary — rider-side ticket activation events |
| `retail_ticket_purchases.csv` | 2.9 GB | 3,530,890 | Primary — purchase transactions |
| `retail_tickets.csv` | 6.0 GB | 6,464,116 | Bridge table — `ticket_id` ↔ `account_id` |
| `validation_scans.csv` | 6.1 GB | 6,371,275 | Primary — handheld inspector scans |
| `calendar_of_events.csv` | small | — | Enrichment — service disruptions, holidays, school days |
| `commuter_rail_stops.csv` | small | — | Reference — stop coordinates and line membership |
| `commuter_rail_boardings_by_line.csv` | small | — | Reference — daily boardings by line |

The two reference tables are loaded but not joined to the per-rider
feature table in this implementation — see Section 2 of
[`Final Report.docx`](Final%20Report.docx) for the geo-aware follow-up
they support.

## Schema (canonical fields used)

### `retail_activations.csv`

| Column | Type | Description |
|---|---|---|
| `account_id` | string | Pseudonymous rider identifier |
| `server_timestamp` | string (ISO 8601) | Activation event time |
| `ticket_id` | string | Foreign key to `retail_tickets` |
| `purchase_id` | string | Foreign key to `retail_ticket_purchases` |
| `purchase_timestamp` | string (ISO 8601) | Denormalised purchase time |

### `retail_ticket_purchases.csv`

| Column | Type | Description |
|---|---|---|
| `account_id` | string | Pseudonymous rider identifier |
| `server_timestamp` | string (ISO 8601) | Purchase event time |
| `purchase_id` | string | Primary key |

### `retail_tickets.csv` (bridge table)

| Column | Type | Description |
|---|---|---|
| `ticket_id` | string | Primary key |
| `account_id` | string | Owner |
| `purchase_id` | string | Originating purchase |
| `product_fare_type` | string | Fare class |
| `product_name` | string | Ticket product name |

### `validation_scans.csv`

| Column | Type | Description |
|---|---|---|
| `user_id` | string | Pseudonymous rider identifier (same space as `account_id`) |
| `server_timestamp` | string (ISO 8601) | Scan event time |
| `activation_timestamp` | string (ISO 8601) | Denormalised activation time of the scanned ticket |
| `ticket_id` | string | Foreign key to `retail_tickets` |
| `application_name` | string | `inspect-ios` or `inspect-jvm` (handheld inspector device) |
| `action_name` | string | Scan action |

### `calendar_of_events.csv`

| Column | Type | Description |
|---|---|---|
| `servicedate` | date | Service date (Eastern time) |
| `has_*` | bool | Flags: `has_holiday`, `has_school_day`, `has_event`, etc. |

## Timestamp handling

All timestamps in the production CSVs are naive local times in
`America/New_York`. The pipeline (in [`UC2_v2/src/uc2_io.py`](UC2_v2/src/uc2_io.py))
localises them to Eastern Time and converts to UTC before any timing
arithmetic, with strict tolerances on parse failures (default ≤ 0.1 % of
rows allowed to fail before the read errors out).

## Data-readiness checklist

A nine-checkpoint readiness audit precedes any modelling. The status from
the latest production run is documented in
[`UC2_v2/RUN_RESULTS.md`](UC2_v2/RUN_RESULTS.md).

## Running the pipeline against synthetic data

If you do not have access to the Masabi dataset but want to verify the
pipeline runs end-to-end, generate synthetic CSVs with the matching
schema:

```bash
python scripts/generate_synthetic_data.py --out UC2_v2/data/ --riders 1000
```

The synthetic generator produces small CSVs with the schema above and a
plausible activation–scan timing distribution. The fitted HMM and
shortlists from a synthetic run are obviously not interpretable — the
purpose is solely to exercise the code path.

## Outputs that contain rider identifiers

The following pipeline outputs contain `account_id`s and **must not be
published**. They are excluded from this repository via
[`.gitignore`](.gitignore):

- `UC2_v2/outputs/feature_table.{parquet,pkl}`
- `UC2_v2/outputs/symbol_rows.{parquet,pkl}`
- `UC2_v2/outputs/sequences.npz`
- `UC2_v2/outputs/rider_scores.parquet`
- `UC2_v2/outputs/uc2_human_review_shortlist_v2.csv`
- `UC2_v2/outputs/uc2_hmm_only_riders.csv`
- `UC2_v2/outputs/uc2_rule_vs_hmm_overlap.csv`

Only model-level artefacts (`hmm_best.pkl`, `hmm_emissions.csv`,
`hmm_grid_results.csv`) — which contain trained parameters but no rider
identifiers — are committed to the repository.
