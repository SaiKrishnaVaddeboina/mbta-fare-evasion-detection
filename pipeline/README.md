# UC2 — Inspector-Triggered Ticket-Purchase Fraud Detection

A reproducible pipeline for identifying mobile-ticket accounts whose activation
behaviour is consistent with inspector-triggered ticket purchases (buying a
ticket only when a fare inspector is about to check it). The pipeline ingests
the mobile-ticketing activation, purchase, and validation-scan logs, derives a
compact symbolic representation of each rider's activation behaviour, fits a
Hidden Markov Model over those symbols, and produces two complementary
human-review shortlists: one that combines rule-based and model-based signal,
and a supplementary shortlist of riders the rules would have missed.

## Project layout

```
pipeline/
├── README.md                 ← this file
├── RUN_RESULTS.md            ← reproducible results from the latest run
├── notebooks/
│   ├── 01_Feature_Engineering.ipynb
│   ├── 02_HMM_Training.ipynb
│   ├── 03_Anomaly_Scoring.ipynb
│   └── 04_Rule_Based_Validation.ipynb
├── src/                      ← importable Python modules
│   ├── uc2_symbols.py        ← 7-symbol activation vocabulary
│   ├── uc2_features.py       ← pattern windows, sequence prep, aggregates
│   ├── uc2_hmm_utils.py      ← parallel multi-seed / multi-state HMM training
│   ├── uc2_scoring.py        ← posterior-dominance scoring + burst de-weight
│   └── uc2_io.py             ← CSV readers, UTC validation, enrichment joins
├── data/                     ← input CSVs (see "Data location")
├── docs/
│   └── UC2_v2_Report.docx    ← final written report
└── outputs/                  ← parquet / pickle / csv deliverables
```

## Pipeline

The four notebooks run sequentially. Each consumes the outputs of the previous
stage and writes its own artefacts to `outputs/`.

| # | Notebook | Input | Output |
|---|---|---|---|
| 1 | `01_Feature_Engineering.ipynb` | Activation, purchase, validation, and enrichment CSVs | `feature_table.parquet`, `symbol_rows.parquet`, `sequences.npz` |
| 2 | `02_HMM_Training.ipynb` | `sequences.npz` | `hmm_best.pkl`, `hmm_emissions.csv`, `hmm_grid_results.csv` |
| 3 | `03_Anomaly_Scoring.ipynb` | all of the above | `rider_scores.parquet`, `uc2_human_review_shortlist_v2.csv` |
| 4 | `04_Rule_Based_Validation.ipynb` | all of the above | `uc2_rule_vs_hmm_overlap.csv`, `uc2_hmm_only_riders.csv` |

Notebook 01 cell 2 resolves the data directory automatically via `UC2Paths` —
see *Data location* below.

## Methodology summary

### Rule-based pattern windows

A rider satisfies the **HIGH** pattern when their activation stream contains
three or more gaps of 15 seconds or less within any sliding 240-hour window,
and the **MEDIUM** pattern when it contains three or more gaps of 30 seconds
or less within any sliding 168-hour window. Both counts are computed directly
from the sorted per-account activation series using an O(n) two-pointer sweep
in `uc2_features.derive_pattern_counts`, so the counts are exact by
construction.

### Activation symbol vocabulary

Each activation event is mapped to one of seven observation symbols in
`uc2_symbols.emit_symbol`, using a first-match-wins rule order:

| ID | Symbol | Rule |
|---|---|---|
| 0 | `ACTIVATE_FAST_HANDHELD` | handheld scan within 15 s of activation |
| 1 | `ACTIVATE_GAMING_THRESHOLD` | handheld scan 16 – 30 s after activation |
| 2 | `ACTIVATE_SLOW_HANDHELD` | handheld scan 30 – 300 s after activation |
| 3 | `ACTIVATE_GATE` | gate scan within 120 s and no fast handheld |
| 4 | `NO_HANDHELD_FOLLOWUP` | no handheld or gate scan in observation window |
| 5 | `OTHER_HANDHELD_PATTERN` | fall-through catch-all |
| 6 | `PURCHASE_THEN_ACTIVATE_FAST` | purchase ≤ 60 s before activation and handheld ≤ 15 s after |

The gate symbol preserves the gate-validation pathway. Repeat-offender signal
is represented at the rider-aggregate level (`repeat_offender_flag`,
`max_infractions_240h`) rather than doubling it in the symbol stream.

### HMM training

`uc2_hmm_utils.train_multi` sweeps `n_components ∈ {7, 9, 11}` across eight
seeds (0 – 7), yielding 24 independent fits. Fits run in parallel through a
`ProcessPoolExecutor` with the fork multiprocessing context on two-thirds of
available CPU cores. Model selection uses BIC. The pipeline uses
`hmmlearn.CategoricalHMM` when available and falls back to a pure-numpy
Baum-Welch implementation otherwise.

### Eligibility and sequence preparation

Only riders with at least five activation events enter HMM training
(`MIN_EVENTS_FOR_HMM = 5`). Each rider's symbol stream is capped FIFO at the
30 most-recent symbols (`MAX_SEQUENCE_LEN = 30`) so long-tenured accounts do
not swamp the likelihood.

### Anomaly scoring

Riders are ranked by **posterior dominance** on a high-risk state set. The
forward-backward algorithm produces smoothed state posteriors for each rider,
and the rider's score is the mean mass those posteriors place on the
high-risk states (selected post-hoc by inspecting the fitted emission
matrix). The final combined score blends four normalised components —
posterior dominance (0.50), rule-violation count (0.30), gaming-band ratio
(0.15), and burst count (0.05) — then applies a proportional penalty to
burst-only riders (≥ 50 bursts but < 3 real infractions) so that burst-only
accounts do not crowd out the shortlist.

### Rule-vs-HMM validation

Notebook 04 computes the overlap of the rule-flagged population *R* and the
top-100 combined shortlist *H*, and separately builds a supplementary
shortlist of the top-100 riders by posterior dominance among those with
**zero** rule infractions. The supplementary shortlist is the evidence that
the HMM surfaces a population the heuristic rules would not have caught.

### Enrichment data

Three reference tables augment the primary feed. `calendar_of_events` is
joined in notebook 01 step 7 so each rider inherits mean exposure to service
disruptions, maintenance windows, events, school days, and holidays — this
protects the shortlist from false positives driven by legitimate gap-looking
behaviour on disruption days. `commuter_rail_stops` and
`commuter_rail_boardings_by_line` are **loaded-not-joined** in this run:
`uc2_io` reads and validates them, but they are not joined to the per-rider
feature table because the current activation feed carries no `station_id` or
`line_id` column. They are retained for the geo-aware follow-up described in
Section 2 of the final report.

## Dependencies

```
pandas     >= 2.0
numpy      >= 1.24
pyarrow    >= 11       # parquet I/O
hmmlearn   >= 0.3      # optional — a pure-numpy fallback ships with the repo
```

Install on a fresh environment:

```
pip install pandas numpy pyarrow hmmlearn
```

## Data location

The notebooks expect the input CSVs in a `data/` directory inside `pipeline/`:

```
pipeline/
└── data/
    ├── retail_activations.csv            (~5.2 GB, 6.4 M rows)  ← required
    ├── retail_ticket_purchases.csv       (~2.9 GB, 3.5 M rows)  ← required
    ├── retail_tickets.csv                (~6.0 GB, 6.5 M rows)  ← required (bridge)
    ├── validation_scans.csv              (~6.1 GB, 6.4 M rows)  ← required
    ├── calendar_of_events.csv                                    ← required (feature)
    ├── commuter_rail_stops.csv                                   ← reference only
    └── commuter_rail_boardings_by_line.csv                       ← reference only
```

The two commuter-rail tables marked *reference only* are read by `uc2_io`
for validation and future use; the current pipeline does not consume them
as per-rider features (see *Enrichment data* above).

Cell 2 of notebook 01 resolves the root automatically; override by editing
`paths = UC2Paths(base=...)` in the same cell if the CSVs live elsewhere.

### Working set size (≈ 20 GB)

The full input is roughly 5.2 GB activations, 2.9 GB purchases, 6.0 GB
tickets, and 6.1 GB scans. On a 16 GB RAM machine, `src/uc2_io.py` reads each
CSV in 1 M-row chunks with id columns cast to `category` dtype, keeping peak
RAM under ≈ 12 GB. See `RUN_RESULTS.md` for per-table memory budget and
wall-time figures from the latest run.

## Design decisions

* **Burst symbols live at the aggregate level.** Repeat-offender behaviour is
  already captured by `repeat_offender_flag` and `max_infractions_240h`; a
  dedicated observation symbol for it would double-count.
* **State grid of {7, 9, 11}.** Eight seeds per state count (24 fits total)
  gives the likelihood landscape enough coverage to reduce seed-sensitivity
  while staying tractable on commodity hardware at two-thirds of CPU cores.
* **Posterior-dominance threshold 0.3** for labelling a rider posterior-driven
  on the shortlist — tuned to the top-100 cut.
* **Burst de-weight factor 0.25** — a 350-burst rider with only 2 fast events
  has their combined score pulled down by ≈ 2.6 pre-normalisation, enough to
  drop them out of the top-100 while leaving hybrid riders intact.

## Reproducibility

Every notebook is deterministic given a fixed random seed. `train_multi` uses
seeds 0 – 7 explicitly. The parallel executor adds non-determinism only to
log-line ordering, not to model selection.
