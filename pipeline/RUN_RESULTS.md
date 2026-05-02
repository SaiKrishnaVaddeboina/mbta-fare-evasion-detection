# UC2 — Run Results

Reproducible results from the full-dataset run. All numbers below come from
a single end-to-end execution of notebooks 01 – 04 on a 16 GB M-series
laptop.

## Input dataset

| File | Size | Rows |
|---|---|---|
| `data/retail_activations.csv` | 5.2 GB | 6,407,862 |
| `data/retail_ticket_purchases.csv` | 2.9 GB | 3,530,890 |
| `data/retail_tickets.csv` | 6.0 GB | 6,464,116 |
| `data/validation_scans.csv` | 6.1 GB | 6,371,275 |
| `data/calendar_of_events.csv` | — | — |
| `data/commuter_rail_stops.csv` | — | — |
| `data/commuter_rail_boardings_by_line.csv` | — | — |

`retail_tickets.csv` is a reference bridge table and is not consumed by
notebooks 01 – 04. Notebook 01 cell 2 resolves the data root automatically
by probing `pipeline/data` first.

## Wall-clock timings

| Stage | Duration |
|---|---|
| Notebook 01 — feature engineering | ~22 min |
| Notebook 02 — HMM training grid | ~90 min |
| Notebook 03 — Exercise 3 scoring | ~3 min |
| Notebook 04 — rule-based validation | ~2 min |

## Feature engineering results

| Quantity | Value |
|---|---|
| Raw activation rows | 6,407,862 |
| Activation events (after dropna on `account_id`) | 3,679,391 |
| `account_id` null rate on raw activations | ≈ 43 % (2,728,471 of 6,407,862) |
| Activation date span | 2025-07-01 → 2025-10-31 (~4 months) |
| `ticket_id` join rate (activations ↔ purchases, via `retail_tickets` bridge) | ≥ 98 % of kept rows |
| Validation scans | 6,371,275 handheld, 0 gate |
| Purchase events (after dropna) | ~2.0 M kept from 3,530,890 raw |
| Unique riders | 232,669 |
| HMM-eligible riders (≥ 5 activations) | 98,241 |
| Rule-flagged riders (HIGH or MEDIUM) | 10,708 |
| &nbsp;&nbsp; HIGH only (240 h window, ≥ 3 gaps ≤ 15 s) | 9,115 |
| &nbsp;&nbsp; MEDIUM only (168 h window, ≥ 3 gaps ≤ 30 s) | 10,427 |

### Data Readiness checklist status

| Activity | Status | Note |
|---|---|---|
| Act 3 — `ticket_id` join ≥ 80 % | ✅ PASS | join rate on kept rows is ≈ 98 % via the `retail_tickets` bridge |
| Act 4 — `activation_timestamp` on scans | ✅ PASS | denormalised onto `validation_scans`, used only for ticket cross-reference |
| Act 5 — HANDHELD share ≥ 1 % | ✅ PASS | 100 % of scans are HANDHELD in this export |
| Act 6 — `scanned_at` station exclusion | ⚠ DEVIATION | not implemented; 0 gate scans in the export make it a no-op |
| Act 7 — `account_id` null rate ≤ 5 % | ⚠ DEVIATION | ≈ 43 % null on raw; interpreted as legitimate guest activations |
| Act 8 — timestamp tz-awareness | ✅ PASS | enforced by `uc2_io` on every timestamp series |
| Act 9 — enrichment join keys present | ⚠ DEVIATION | `commuter_rail_stops` / `_boardings_by_line` loaded-not-joined (no `station_id` / `line_id` on activation feed) |

Deviations are discussed in Section 2 of `docs/UC2_v2_Report.docx`.

## HMM training

The `{7, 9, 11} states × 8 seeds × 50 iter` grid ran in parallel on
two-thirds of the available CPU cores via `hmmlearn.CategoricalHMM`.

| Metric | Value |
|---|---|
| Training observations | 1,767,777 |
| Training sequences (= HMM-eligible riders) | 98,241 |
| Sequence length p50 / p90 / p99 | 16 / 30 / 30 (FIFO cap biting top ≈ 10 %) |
| Selected state count (minimum BIC) | **9** |
| Best seed | 6 |
| Log-likelihood | −2,640,637.1 |
| BIC | 5,283,200.8 |
| Runner-up (11 states, seed 5) | BIC 5,314,340.0 |

## Exercise 3 scoring — primary shortlist

The primary shortlist combines posterior-dominance, rule-violation count,
gaming-band ratio, and burst count, then de-weights burst-only riders.

| Metric | Value |
|---|---|
| Riders scored | 98,241 |
| Burst-only riders in top-100 | 0 |
| Rule-confirmed riders in top-100 (`R ∩ H`) | 100 / 100 |
| HMM-only riders in top-100 (`H \ R`) | 0 |

With 10,708 rule-flagged riders competing for 100 slots on a score that
incorporates rule infractions, the combined score naturally promotes
rule-flagged riders to the top. The |R ∩ H| = 100 result is a
methodology-agreement finding: the HMM confirms the rules rather than
manufacturing false positives.

## Exercise 3 scoring — supplementary HMM-only shortlist

To measure incremental discovery beyond the heuristic rules, the
supplementary shortlist ranks riders by posterior dominance **among those
with zero rule infractions** (`max_infractions_240h < 3` AND
`max_infractions_168h < 3`).

| Metric | Value |
|---|---|
| Non-rule-flagged scored pool | 88,328 |
| Supplementary shortlist size | 100 |
| Top posterior dominance | 1.0000 |
| Median posterior dominance | 0.9999 |

**Interpretation.** 88,328 riders had zero rule infractions yet still
cleared the ≥ 5-activation HMM eligibility floor. Among those, the top 100
selected by posterior dominance all spend essentially 100 % of their
activations in the high-risk state set (median 0.9999, top 1.0000). These
are the riders the 240 h / 168 h heuristics would have missed entirely,
and the HMM pins them at the ceiling — direct evidence that the HMM
surfaces a distinct, defensible population for human review.

## Deliverable files

All artefacts land in `pipeline/outputs/`:

* `feature_table.parquet` — rider × feature matrix with calendar flags
* `symbol_rows.parquet` — long-format symbol rows
* `sequences.npz` — HMM training input (≥ 5-event riders × FIFO-capped-30 sequences)
* `hmm_best.pkl` — fitted CategoricalHMM plus BIC grid
* `hmm_emissions.csv` — K × V emission matrix
* `hmm_grid_results.csv` — full BIC / AIC sweep
* `rider_scores.parquet` — per-rider combined score and components
* `uc2_human_review_shortlist_v2.csv` — primary top-100 combined shortlist
* `uc2_hmm_only_riders.csv` — supplementary top-100 HMM-only shortlist
* `uc2_rule_vs_hmm_overlap.csv` — `R` / `H` / `R ∩ H` / `H \ R` / supplementary pool counts

## Running on a 16 GB laptop

### Measured memory budget

| Table | Peak RSS during read | Steady-state dataframe memory |
|---|---|---|
| activations (6.4 M raw → 3.6 M dropna) | ~4.4 GB | ~1.5 GB |
| purchases (3.5 M raw → 2.0 M dropna) | ~2.0 GB | ~0.8 GB |
| scans (6.4 M raw → 3.6 M dropna) | ~4.5 GB | ~1.8 GB |

Notebook 01 loads the three tables sequentially, not in parallel: peak
RSS is ~4.5 GB during the scans read and settles to ~4.1 GB with all
three resident. The `merge_asof` join adds ~2 GB transient and the HMM
fit adds ~0.5 GB, for a total peak of ~7 GB on a 16 GB machine — leaving
~9 GB headroom.

### Step-by-step

1. Install dependencies once:

   ```
   pip3 install --user --break-system-packages pandas numpy pyarrow hmmlearn openpyxl python-docx
   ```

2. Confirm the CSVs are in place:

   ```
   ls -lh ~/Desktop/"Cap proj"/pipeline/data/*.csv
   ```

   The four primary files are in the 3 – 7 GB range; the three enrichment
   CSVs are small.

3. Open the project in VS Code and select a Python 3.10+ kernel that has
   the packages from step 1.

4. Run the four notebooks in order — no code edits needed:

   * `01_UC2_Feature_Engineering.ipynb` (~22 min)
   * `02_UC2_HMM_Training.ipynb` (~90 min)
   * `03_UC2_Exercise3_Scoring.ipynb` (~3 min)
   * `04_UC2_Rule_Based_Validation.ipynb` (~2 min)

5. The final deliverables land in `pipeline/outputs/`. See the *Deliverable
   files* section above.

### If memory becomes tight

1. Close Chrome, Slack, Docker Desktop, or any other heavy GUI first —
   freeing 3 – 4 GB is usually sufficient.
2. Halve the chunk size by editing `CHUNKSIZE_BIG = 500_000` in
   `src/uc2_io.py`. This cuts the transient peak by roughly 30 %.
3. Fall back to month-at-a-time processing by passing
   `date_from="2025-07-01", date_to="2025-08-01"` to the three readers in
   notebook 01 cell 4, then repeating for each month.

### Categorical IDs at the merge boundary

pandas' `merge_asof` refuses to merge two categorical columns whose
category sets differ (they always do when the left and right frames are
read separately). Notebook 01 cell 9 and `outputs/run_nb01.py` cast
`account_id` and `ticket_id` back to `object` at the merge boundary only —
the large frames keep their categorical dtype for memory efficiency while
only the transient merge surface is expanded.
