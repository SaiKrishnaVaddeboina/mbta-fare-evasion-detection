"""
UC2 Feature Engineering helpers.

Computes per-rider features used by the HMM training and scoring stages:

  - Pattern HIGH (>=3 gaps <=15s inside a sliding 240h window) and
    Pattern MEDIUM (>=3 gaps <=30s inside a sliding 168h window)
    infraction counts, via an O(n) two-pointer sweep.
  - Timing aggregates (mean/median/min/max gap, near-threshold ratio).
  - Symbol-sequence preparation for HMM training, with a minimum-
    eligibility floor of 5 activations and a FIFO cap of 30 symbols
    per rider.

All timing features are derived from validated tz-aware UTC timestamps.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import numpy as np
import pandas as pd


# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

HIGH_GAP_S:      int = 15
HIGH_WINDOW_S:   int = 240 * 3600     # 240h == 10 days
HIGH_MIN_COUNT:  int = 3

MED_GAP_S:       int = 30
MED_WINDOW_S:    int = 168 * 3600     # 168h == 7 days
MED_MIN_COUNT:   int = 3

MIN_EVENTS_FOR_HMM: int = 5           # minimum-activation eligibility floor
MAX_SEQUENCE_LEN:   int = 30          # FIFO cap on per-rider symbol history


# -----------------------------------------------------------------------------
# Pattern-window counters
# -----------------------------------------------------------------------------

def _max_in_window(
    timestamps: np.ndarray,
    gaps: np.ndarray,
    gap_threshold_s: float,
    window_s: float,
    min_count: int,
) -> int:
    """Return the maximum number of qualifying gaps found inside any
    sliding window of ``window_s`` seconds.

    A gap qualifies when ``gaps[i] <= gap_threshold_s``. Windows are
    anchored on each qualifying event and extend forward.

    Implementation is O(n) because ``timestamps`` is assumed sorted.
    """
    if len(timestamps) < min_count:
        return 0

    qualifying = timestamps[gaps <= gap_threshold_s]
    if qualifying.size < min_count:
        return 0

    # two-pointer sweep
    left = 0
    best = 0
    for right in range(qualifying.size):
        while qualifying[right] - qualifying[left] > window_s:
            left += 1
        best = max(best, right - left + 1)
    return best


def derive_pattern_counts(events: pd.DataFrame) -> pd.DataFrame:
    """Aggregate per-account Pattern HIGH / MEDIUM infraction counts.

    Parameters
    ----------
    events : DataFrame with columns
        - account_id  : str
        - activate_ts : float (epoch seconds, sorted per account)
        - gap_s       : float (seconds from prior activation within the
                        same account; NaN for the first event)

    Returns
    -------
    DataFrame indexed by account_id with columns:
        max_infractions_240h  (HIGH 240h window)
        max_infractions_168h  (MEDIUM 168h window)
        has_pattern_high      bool
        has_pattern_medium    bool
    """
    records = []
    for account_id, grp in events.sort_values("activate_ts").groupby("account_id", observed=True):
        ts = grp["activate_ts"].to_numpy()
        gaps = grp["gap_s"].fillna(np.inf).to_numpy()

        high = _max_in_window(ts, gaps, HIGH_GAP_S, HIGH_WINDOW_S, HIGH_MIN_COUNT)
        med  = _max_in_window(ts, gaps, MED_GAP_S,  MED_WINDOW_S,  MED_MIN_COUNT)

        records.append({
            "account_id":           account_id,
            "max_infractions_240h": high,
            "max_infractions_168h": med,
            "has_pattern_high":     high >= HIGH_MIN_COUNT,
            "has_pattern_medium":   med  >= MED_MIN_COUNT,
        })
    return pd.DataFrame.from_records(records).set_index("account_id")


# -----------------------------------------------------------------------------
# Sequence preparation
# -----------------------------------------------------------------------------

@dataclass
class SequenceBatch:
    """Container for HMM training input."""
    account_ids: list[str]
    sequences:   list[np.ndarray]      # each sequence is int symbols
    lengths:     np.ndarray            # shape (n_riders,)

    @property
    def concatenated(self) -> np.ndarray:
        """hmmlearn expects a 2-D column of concatenated observations."""
        return np.concatenate(self.sequences).reshape(-1, 1)


def prepare_sequences(
    symbol_rows: pd.DataFrame,
    min_events: int = MIN_EVENTS_FOR_HMM,
    max_len:    int = MAX_SEQUENCE_LEN,
) -> SequenceBatch:
    """Build a SequenceBatch from a long-format symbol table.

    Parameters
    ----------
    symbol_rows : DataFrame with columns
        - account_id  : str
        - activate_ts : float (used only for ordering)
        - symbol_id   : int
    min_events : eligibility floor (default 5)
    max_len    : FIFO cap so long-tenured riders don't over-contribute
                 (FIFO cap, default 30)
    """
    ids:    list[str]        = []
    seqs:   list[np.ndarray] = []
    lengths: list[int]       = []

    for account_id, grp in symbol_rows.sort_values("activate_ts").groupby("account_id", observed=True):
        if len(grp) < min_events:
            continue
        seq = grp["symbol_id"].to_numpy()
        if len(seq) > max_len:
            seq = seq[-max_len:]          # FIFO keeps the most-recent N
        ids.append(account_id)
        seqs.append(seq)
        lengths.append(len(seq))

    return SequenceBatch(
        account_ids=ids,
        sequences=seqs,
        lengths=np.asarray(lengths, dtype=int),
    )


# -----------------------------------------------------------------------------
# Near-threshold / gaming ratios
# -----------------------------------------------------------------------------

def derive_timing_aggregates(events: pd.DataFrame) -> pd.DataFrame:
    """Compute mean/median/min/max gap and near-threshold ratio.

    near_threshold_ratio = fraction of activations whose gap_s falls in
    the GAMING band (16-30s). Riders who strategically dodge the 15s
    cutoff show up here.
    """
    def _per_group(grp: pd.DataFrame) -> pd.Series:
        gap = grp["gap_s"].dropna()
        if gap.empty:
            return pd.Series({
                "mean_gap_seconds":     np.nan,
                "median_gap_seconds":   np.nan,
                "min_gap_seconds":      np.nan,
                "max_gap_seconds":      np.nan,
                "near_threshold_ratio": 0.0,
            })
        near = ((gap > 15) & (gap <= 30)).mean()
        return pd.Series({
            "mean_gap_seconds":     gap.mean(),
            "median_gap_seconds":   gap.median(),
            "min_gap_seconds":      gap.min(),
            "max_gap_seconds":      gap.max(),
            "near_threshold_ratio": near,
        })

    return events.groupby("account_id", observed=True).apply(_per_group)


__all__ = [
    "HIGH_GAP_S", "HIGH_WINDOW_S", "HIGH_MIN_COUNT",
    "MED_GAP_S", "MED_WINDOW_S", "MED_MIN_COUNT",
    "MIN_EVENTS_FOR_HMM", "MAX_SEQUENCE_LEN",
    "derive_pattern_counts",
    "prepare_sequences",
    "derive_timing_aggregates",
    "SequenceBatch",
]
