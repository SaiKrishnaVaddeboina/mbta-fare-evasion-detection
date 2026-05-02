"""
Generate remaining report figures + shortlist stability analysis.

Run this from the pipeline/ directory with your notebook's Python:

    cd ~/Desktop/"Cap proj"/pipeline
    python3 make_figures.py

It reads rider_scores.parquet, hmm_best.pkl, sequences.npz and writes:
    docs/figures/score_histogram.png
    docs/figures/shortlist_stability.png
    docs/figures/stability_summary.json

Expected runtime: 10 – 15 min (dominated by re-decoding 98k sequences
through each of the six near-winning HMM fits).
"""

from __future__ import annotations

import json
import pickle
import sys
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

sys.path.insert(0, str(ROOT / "src"))
from uc2_scoring import (
    posterior_state_dominance,
    identify_high_risk_states,
    combined_anomaly_score,
)
from uc2_symbols import SYMBOLS


# =============================================================================
# 1. Combined-score histogram
# =============================================================================

scores = pd.read_parquet(OUT / "rider_scores.parquet")
print(f"Loaded {len(scores):,} scored riders")

vals = scores["combined_anomaly_score"].values
vals_nonzero = vals[vals > 0]
top100_cutoff = np.sort(vals)[-100]

fig, ax = plt.subplots(figsize=(7.8, 3.8), dpi=160)
ax.hist(
    vals_nonzero, bins=80, color=NAVY, alpha=0.85,
    edgecolor="white", linewidth=0.4,
)
ax.axvline(
    top100_cutoff, color=ACCENT, linewidth=1.8, linestyle="--", zorder=5,
    label=f"Top-100 cutoff = {top100_cutoff:.3f}",
)
ax.set_yscale("log")
ax.set_xlabel("Combined anomaly score")
ax.set_ylabel("Number of riders (log scale)")
ax.set_title(f"Combined Anomaly Score Distribution ({len(scores):,} scored riders)")
ax.legend(loc="upper right", frameon=False, fontsize=9)
ax.grid(axis="y", color=GREY, alpha=0.3, linewidth=0.5)
ax.set_axisbelow(True)
for spine in ("top", "right"):
    ax.spines[spine].set_visible(False)
for spine in ("left", "bottom"):
    ax.spines[spine].set_color(GREY)
plt.tight_layout()
plt.savefig(FIG / "score_histogram.png", dpi=160, bbox_inches="tight")
plt.close()
print(f"Wrote {FIG / 'score_histogram.png'}")


# =============================================================================
# 2. Shortlist stability across winner + top near-winners (Jaccard)
#
# hmm_best.pkl persists only the BIC-winning model. For the stability
# analysis we refit the two closest runner-ups here and compare their
# top-100 shortlists against the winner's.
# =============================================================================

with open(OUT / "hmm_best.pkl", "rb") as f:
    bundle = pickle.load(f)

winner_model = bundle["model"]
all_results = bundle["all_results"]

# Sort by BIC ascending, keep top 3 (winner + 2 runner-ups)
ranked = sorted(all_results, key=lambda r: r.bic)[:3]
print("Top-3 fits by BIC:")
for i, r in enumerate(ranked):
    print(f"  #{i+1}  states={r.n_components}, seed={r.seed}, BIC={r.bic:,.0f}")

# Load sequences (same split as notebook 03)
z = np.load(OUT / "sequences.npz", allow_pickle=True)
account_ids = z["account_ids"]
concatenated = z["concatenated"].ravel()
lengths = z["lengths"]

sequences = []
offset = 0
for L in lengths:
    sequences.append(concatenated[offset:offset + L])
    offset += L
print(f"Prepared {len(sequences):,} rider sequences")

# Re-training helper (import the internal builder to avoid duplicating logic)
from uc2_hmm_utils import _fit_one  # type: ignore
from uc2_symbols import N_SYMBOLS

X = concatenated.reshape(-1, 1)

# Build a list: (label, model) for winner + refit runner-ups
fit_models = [(ranked[0], winner_model)]
for r in ranked[1:]:
    print(f"Refitting states={r.n_components}, seed={r.seed} ...")
    res, hmm = _fit_one(
        n_components=r.n_components,
        n_features=N_SYMBOLS,
        seed=r.seed,
        X=X,
        lengths=lengths,
        n_iter=50,
    )
    print(f"  done, LL={res.log_likelihood:,.1f}, BIC={res.bic:,.1f}")
    fit_models.append((r, hmm))

fraud_symbols = [
    SYMBOLS["ACTIVATE_FAST_HANDHELD"],
    SYMBOLS["ACTIVATE_GAMING_THRESHOLD"],
    SYMBOLS["PURCHASE_THEN_ACTIVATE_FAST"],
]

feat = scores.loc[account_ids].copy()
rule_viol = (feat["max_infractions_240h"].to_numpy()
             + feat["max_infractions_168h"].to_numpy())
gaming_ratio = feat["near_threshold_ratio"].to_numpy()
burst_cnt = feat.get(
    "n_OTHER_HANDHELD_PATTERN", pd.Series(0, index=feat.index)
).to_numpy()
fast_cnt = feat.get(
    "n_ACTIVATE_FAST_HANDHELD", pd.Series(0, index=feat.index)
).to_numpy()
gaming_cnt = feat.get(
    "n_ACTIVATE_GAMING_THRESHOLD", pd.Series(0, index=feat.index)
).to_numpy()

shortlists = []
for ix, (res, model) in enumerate(fit_models):
    label = (
        f"#{ix+1}  states={res.n_components}, seed={res.seed}, "
        f"BIC={res.bic:,.0f}"
    )
    high_risk_states = identify_high_risk_states(model, fraud_symbols)
    pd_vec = posterior_state_dominance(model, sequences, high_risk_states)
    combined = combined_anomaly_score(
        posterior_dominance=pd_vec,
        rule_violation_count=rule_viol,
        gaming_ratio=gaming_ratio,
        burst_counts=burst_cnt,
        fast_counts=fast_cnt,
        gaming_counts=gaming_cnt,
    )
    order = np.argsort(-combined)[:100]
    top_ids = set(account_ids[order])
    shortlists.append((label, top_ids))
    print(f"  {label} → high_risk_states={high_risk_states}, top-100 built")

labels = [s[0] for s in shortlists]
short_labels = [f"#{i+1}" for i in range(len(shortlists))]
k = len(shortlists)
J = np.zeros((k, k))
for i in range(k):
    for j in range(k):
        a, b = shortlists[i][1], shortlists[j][1]
        J[i, j] = len(a & b) / len(a | b) if len(a | b) else 1.0


# Heatmap
fig, ax = plt.subplots(figsize=(7.6, 5.6), dpi=160)
im = ax.imshow(J, vmin=0, vmax=1, cmap="Blues")
ax.set_xticks(range(k))
ax.set_yticks(range(k))
ax.set_xticklabels(short_labels, fontsize=9)
ax.set_yticklabels(short_labels, fontsize=9)
ax.set_title(
    f"Top-100 shortlist stability across the {k} lowest-BIC fits "
    f"(Jaccard overlap)",
    fontsize=10,
)
for i in range(k):
    for j in range(k):
        c = "white" if J[i, j] > 0.55 else NAVY
        ax.text(j, i, f"{J[i,j]:.2f}",
                ha="center", va="center", fontsize=9, color=c)
cbar = fig.colorbar(im, ax=ax, shrink=0.85)
cbar.set_label("Jaccard overlap", fontsize=9)

# Legend box underneath
legend_lines = "\n".join(labels)
fig.text(
    0.02, -0.01, legend_lines,
    fontsize=7.5, color=NAVY,
    family="monospace",
    verticalalignment="top",
)
plt.tight_layout(rect=(0, 0.18, 1, 1))
plt.savefig(FIG / "shortlist_stability.png", dpi=160, bbox_inches="tight")
plt.close()
print(f"Wrote {FIG / 'shortlist_stability.png'}")


# Summary
off_diag = J[~np.eye(k, dtype=bool)]
summary = {
    "n_fits_compared": k,
    "winner": labels[0],
    "median_pairwise_jaccard": float(np.median(off_diag)),
    "min_pairwise_jaccard":    float(off_diag.min()),
    "max_pairwise_jaccard":    float(off_diag.max()),
    "mean_pairwise_jaccard":   float(off_diag.mean()),
    "winner_vs_others_jaccard": [
        {"fit": labels[i], "jaccard_vs_winner": float(J[0, i])}
        for i in range(1, k)
    ],
    "fits": labels,
}
(FIG / "stability_summary.json").write_text(json.dumps(summary, indent=2))
print(f"Wrote {FIG / 'stability_summary.json'}")

print()
print(f"Median pairwise Jaccard: {summary['median_pairwise_jaccard']:.3f}")
print(f"Min pairwise Jaccard   : {summary['min_pairwise_jaccard']:.3f}")
print(f"Max pairwise Jaccard   : {summary['max_pairwise_jaccard']:.3f}")
print(f"Winner vs. runners-up  :")
for item in summary["winner_vs_others_jaccard"]:
    print(f"  {item['jaccard_vs_winner']:.3f}  ←  {item['fit']}")
