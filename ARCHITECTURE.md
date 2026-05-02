# Architecture

How a raw S3 event becomes a deployable risk score, in six stages.

## Pipeline overview

```mermaid
flowchart LR
    subgraph "1. Ingest"
        S3[(AWS S3<br/>masabi-fraud-detection-lab-input)]
        S3 --> A[retail_activations.csv<br/>5.2 GB · 6.4 M rows]
        S3 --> P[retail_ticket_purchases.csv<br/>2.9 GB · 3.5 M rows]
        S3 --> T[retail_tickets.csv<br/>6.0 GB · 6.5 M rows]
        S3 --> V[validation_scans.csv<br/>6.1 GB · 6.4 M rows]
        S3 --> C[calendar_of_events.csv]
    end

    subgraph "2. Feature Engineering"
        A & P & T & V & C --> FE[uc2_features.py<br/>uc2_io.py]
        FE --> SR[symbol_rows.parquet<br/>7-symbol vocabulary]
        FE --> FT[feature_table.parquet<br/>86 features × 14 groups]
        FE --> SQ[sequences.npz<br/>FIFO-30 cap, ≥5 events]
    end

    subgraph "3. HMM Training"
        SQ --> HMM[uc2_hmm_utils.py<br/>train_multi]
        HMM --> GR[hmm_grid_results.csv<br/>BIC sweep over states × seeds]
        HMM --> BM[hmm_best.pkl<br/>9 states, BIC-selected]
        HMM --> EM[hmm_emissions.csv<br/>K × V emission matrix]
    end

    subgraph "4. Composite Scoring"
        BM --> SC[uc2_scoring.py<br/>posterior_state_dominance]
        FT --> SC
        SC --> RS[rider_scores.parquet<br/>30/25/25/20 weighted score]
    end

    subgraph "5. Clustering"
        RS --> CL[K-means K=6<br/>silhouette-validated]
        CL --> CP[6 cluster archetypes]
    end

    subgraph "6. Validation"
        RS --> SH[uc2_human_review_shortlist.csv<br/>top-100 combined]
        RS --> HR[uc2_hmm_only_riders.csv<br/>top-100 HMM-novel]
        SH & HR --> RV[Masabi human review<br/>60-rider sheet]
    end

    classDef confidential fill:#fee,stroke:#c00,stroke-width:1px;
    classDef public fill:#efe,stroke:#080,stroke-width:1px;
    class A,P,T,V,C,SR,FT,SQ,RS,SH,HR confidential
    class GR,BM,EM,CP,RV public
```

Boxes shaded red contain rider identifiers and are excluded from this repository (see [DATA.md](DATA.md) and [.gitignore](.gitignore)). Boxes shaded green contain only model-level artefacts and are committed.

## Module map (`UC2_v2/src/`)

| Module | Responsibility |
|---|---|
| [`uc2_symbols.py`](UC2_v2/src/uc2_symbols.py) | Seven-symbol activation vocabulary; first-match-wins emit rule on a per-event `ActivationEvent` record |
| [`uc2_features.py`](UC2_v2/src/uc2_features.py) | O(n) two-pointer pattern-window counter (HIGH 240 h / MEDIUM 168 h); FIFO-30 sequence prep; timing aggregates |
| [`uc2_hmm_utils.py`](UC2_v2/src/uc2_hmm_utils.py) | `train_multi` parallel multi-seed × multi-state grid; `hmmlearn` primary + pure-numpy Baum-Welch fallback; BIC selection |
| [`uc2_scoring.py`](UC2_v2/src/uc2_scoring.py) | Posterior-state dominance, four-signal weighted composite, burst-only de-weight |
| [`uc2_io.py`](UC2_v2/src/uc2_io.py) | Chunked CSV reads (1 M-row chunks), categorical ID dtypes, strict UTC timestamp validation, calendar enrichment |

## Data-flow contracts

```mermaid
sequenceDiagram
    participant Notebook
    participant uc2_io
    participant uc2_features
    participant uc2_symbols
    participant uc2_hmm_utils
    participant uc2_scoring

    Notebook->>uc2_io: read_activations / read_purchases / read_scans
    uc2_io-->>Notebook: tz-aware UTC dataframes (account_id, *_ts)
    Notebook->>uc2_features: derive_pattern_counts(activations)
    uc2_features-->>Notebook: max_infractions_240h / 168h, pattern flags
    Notebook->>uc2_symbols: emit_symbol(ActivationEvent) per event
    uc2_symbols-->>Notebook: symbol_id ∈ [0, 6]
    Notebook->>uc2_features: prepare_sequences(symbol_rows)
    uc2_features-->>Notebook: SequenceBatch (FIFO-30, ≥5 events)
    Notebook->>uc2_hmm_utils: train_multi(X, lengths, n_features)
    uc2_hmm_utils-->>Notebook: best_model + FitResult grid
    Notebook->>uc2_scoring: combined_anomaly_score(...)
    uc2_scoring-->>Notebook: rider scores after burst de-weight
```

## Notebook → output map

| # | Notebook | Reads | Writes |
|---|---|---|---|
| 01 | `01_UC2_Feature_Engineering.ipynb` | All input CSVs | `feature_table.parquet`, `symbol_rows.parquet`, `sequences.npz` |
| 02 | `02_UC2_HMM_Training.ipynb` | `sequences.npz` | `hmm_best.pkl`, `hmm_emissions.csv`, `hmm_grid_results.csv` |
| 03 | `03_UC2_Exercise3_Scoring.ipynb` | All of the above | `rider_scores.parquet`, `uc2_human_review_shortlist_v2.csv` |
| 04 | `04_UC2_Rule_Based_Validation.ipynb` | All of the above | `uc2_rule_vs_hmm_overlap.csv`, `uc2_hmm_only_riders.csv` |

## Key design decisions

- **Burst symbols at the aggregate level, not the symbol stream.** Repeat-offender behaviour is captured by `repeat_offender_flag` and `max_infractions_240h`; a dedicated observation symbol would double-count.
- **State grid `{7, 9, 11}` × 8 seeds = 24 fits.** Covers the likelihood landscape sufficiently to mitigate seed sensitivity while staying tractable on commodity hardware (≈ 90 min on a 16 GB M-series laptop at 2/3 of CPU cores).
- **Posterior-dominance threshold 0.3** for labelling a rider as posterior-driven on the shortlist — tuned to the top-100 cut.
- **Burst de-weight factor 0.25** — a 350-burst rider with only 2 fast events has their combined score reduced by ≈ 2.6 pre-normalisation, dropping them out of the top-100 while leaving hybrid riders intact.
- **Categorical IDs at the merge boundary.** pandas refuses to `merge_asof` two categorical columns whose category sets differ; we cast back to `object` only at the merge surface and keep large frames categorical for memory.

## Performance characteristics

| Stage | Wall-clock | Peak RSS |
|---|---|---|
| Notebook 01 — feature engineering | ~22 min | ~7 GB |
| Notebook 02 — HMM training (24 fits, parallel) | ~90 min | ~5 GB |
| Notebook 03 — scoring | ~3 min | ~3 GB |
| Notebook 04 — rule-vs-HMM validation | ~2 min | ~3 GB |

Reproducible on a 16 GB M-series laptop. See [UC2_v2/RUN_RESULTS.md](UC2_v2/RUN_RESULTS.md) for the full memory budget.
