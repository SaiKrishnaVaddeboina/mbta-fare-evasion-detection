"""
Weight-sensitivity analysis for the UC2 combined anomaly score.

Addresses LF review point 3.1: 'Your combined score gives the HMM only 5 % weight
... test sensitivity to the weight choices.'

The script recomputes the top-100 shortlist under several weighting schemes and
reports pairwise Jaccard overlap against the baseline (the published weights).
The output is a short JSON summary and a heatmap.

Run this from the UC2_v2/ directory with your notebook's Python:

    cd ~/Desktop/"Cap proj"/UC2_v2
    python3 make_sensitivity.py

It reads:
    outputs/rider_scores.parquet
    outputs/hmm_best.pkl  (for posterior_dominance if not in rider_scores)
and writes:
    docs/figures/sensitivity.png
    docs/figures/sensitivity_summary.json

Runtime: ~30 seconds.
"""

from __future__ import annotations

import json
import sys
from itertools import combinations
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

try:
    import pyarrow  # noqa: F401
except ImportError:
    sys.exit(
        "pyarrow is required. Install with:\n"
        "    pip3 install --user --break-system-packages pyarrow"
    )

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "outputs"
FIG = ROOT / "docs" / "figures"
FIG.mkdir(parents=True, exist_ok=True)

NAVY = "#1F3A68"
ACCENT = "#C8102E"
GREY = "#BFBFBF"


# -----------------------------------------------------------------------------
# Load pre-scored riders
# -----------------------------------------------------------------------------

scores = pd.read_parquet(OUT / "rider_scores.parquet")
print(f"Loaded {len(scores):,} scored riders")

# Component signals used by uc2_scoring.combined_anomaly_score
# Re-derive normalised components so we can recombine under arbitrary weights.
pd_vec = scores["posterior_dominance"].to_numpy()
rule_viol = (
    scores["max_infractions_240h"].to_numpy()
    + scores["max_infractions_168h"].to_numpy()
)
gaming_ratio = scores["near_threshold_ratio"].to_numpy()
burst_cnt = scores.get(
    "n_OTHER_HANDHELD_PATTERN", pd.Series(0, index=scores.index)
).to_numpy()
fast_cnt = scores.get(
    "n_ACTIVATE_FAST_HANDHELD", pd.Series(0, index=scores.index)
).to_numpy()


def _norm(x: np.ndarray) -> np.ndarray:
    """Robust 0-1 normalisation by the 99th percentile (caps outliers)."""
    q99 = np.percentile(x, 99) if len(x) else 1.0
    return np.clip(x / q99, 0.0, 1.0) if q99 > 0 else x


pd_n = _norm(pd_vec)
rule_n = _norm(rule_viol)
gaming_n = _norm(gaming_ratio)
burst_n = _norm(burst_cnt)

# Burst de-weight (same rule as uc2_scoring.combined_anomaly_score)
burst_only = (burst_cnt >= 50) & (rule_viol < 3) & (fast_cnt < 3)
deweight = np.ones_like(pd_n)
deweight[burst_only] = 0.25


def score_under(weights: dict) -> np.ndarray:
    """Combined score given a dict of four weights that sum to 1.0."""
    w = weights
    raw = (
        w["posterior"] * pd_n
        + w["rule"]   * rule_n
        + w["gaming"] * gaming_n
        + w["burst"]  * burst_n
    )
    return raw * deweight


# -----------------------------------------------------------------------------
# Weight schemes to probe
# -----------------------------------------------------------------------------

schemes = {
    "Baseline (0.50 / 0.30 / 0.15 / 0.05)":
        dict(posterior=0.50, rule=0.30, gaming=0.15, burst=0.05),
    "HMM-heavy (0.70 / 0.15 / 0.10 / 0.05)":
        dict(posterior=0.70, rule=0.15, gaming=0.10, burst=0.05),
    "Rule-heavy (0.20 / 0.60 / 0.15 / 0.05)":
        dict(posterior=0.20, rule=0.60, gaming=0.15, burst=0.05),
    "Equal weights (0.25 / 0.25 / 0.25 / 0.25)":
        dict(posterior=0.25, rule=0.25, gaming=0.25, burst=0.25),
    "HMM-only (1.0 / 0 / 0 / 0)":
        dict(posterior=1.0,  rule=0.0,  gaming=0.0,  burst=0.0),
    "Rules-only (0 / 1.0 / 0 / 0)":
        dict(posterior=0.0,  rule=1.0,  gaming=0.0,  burst=0.0),
}

# Build top-100 sets under each scheme
tops: dict[str, set] = {}
for label, weights in schemes.items():
    s = score_under(weights)
    order = np.argsort(-s)[:100]
    tops[label] = set(scores.index[order])
    print(f"  {label}: top-100 built")


# -----------------------------------------------------------------------------
# Pairwise Jaccard + overlap with baseline
# -----------------------------------------------------------------------------

labels = list(schemes.keys())
k = len(labels)
J = np.zeros((k, k))
for i in range(k):
    for j in range(k):
        a, b = tops[labels[i]], tops[labels[j]]
        J[i, j] = len(a & b) / len(a | b) if len(a | b) else 1.0

baseline = labels[0]
summary_rows = []
for i, lbl in enumerate(labels):
    shared = len(tops[baseline] & tops[lbl])
    summary_rows.append({
        "scheme": lbl,
        "weights": schemes[lbl],
        "shared_with_baseline": int(shared),
        "jaccard_vs_baseline": float(J[0, i]),
    })

# Heatmap
fig, ax = plt.subplots(figsize=(8.2, 5.6), dpi=160)
im = ax.imshow(J, vmin=0, vmax=1, cmap="Blues")
ax.set_xticks(range(k))
ax.set_yticks(range(k))
short = [f"S{i+1}" for i in range(k)]
ax.set_xticklabels(short, fontsize=9)
ax.set_yticklabels(short, fontsize=9)
ax.set_title(
    "Top-100 Jaccard overlap across combined-score weighting schemes",
    fontsize=10,
)
for i in range(k):
    for j in range(k):
        c = "white" if J[i, j] > 0.55 else NAVY
        ax.text(j, i, f"{J[i,j]:.2f}",
                ha="center", va="center", fontsize=9, color=c)
cbar = fig.colorbar(im, ax=ax, shrink=0.85)
cbar.set_label("Jaccard overlap", fontsize=9)

legend_lines = "\n".join(f"{s}  {lbl}" for s, lbl in zip(short, labels))
fig.text(
    0.02, -0.01, legend_lines,
    fontsize=7.5, color=NAVY,
    family="monospace",
    verticalalignment="top",
)
plt.tight_layout(rect=(0, 0.22, 1, 1))
plt.savefig(FIG / "sensitivity.png", dpi=160, bbox_inches="tight")
plt.close()
print(f"Wrote {FIG / 'sensitivity.png'}")

# JSON summary
off_diag = J[~np.eye(k, dtype=bool)]
summary = {
    "n_schemes": k,
    "baseline": baseline,
    "median_pairwise_jaccard": float(np.median(off_diag)),
    "min_pairwise_jaccard":    float(off_diag.min()),
    "max_pairwise_jaccard":    float(off_diag.max()),
    "schemes": summary_rows,
}
(FIG / "sensitivity_summary.json").write_text(json.dumps(summary, indent=2))
print(f"Wrote {FIG / 'sensitivity_summary.json'}")

print()
print("Summary:")
for row in summary_rows:
    print(
        f"  jaccard_vs_baseline={row['jaccard_vs_baseline']:.3f}  "
        f"shared={row['shared_with_baseline']:3d}/100  ←  {row['scheme']}"
    )
print(f"\nMedian pairwise Jaccard across all 6 schemes: "
      f"{summary['median_pairwise_jaccard']:.3f}")
