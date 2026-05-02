"""
UC2 rider anomaly scoring.

Implements two scoring components:

  Posterior dominance
    For each rider, the mean smoothed posterior mass the fitted HMM
    places on a post-hoc-selected high-risk state set. Replaces any
    raw log-likelihood ranking.

  Burst-only de-weight
    Riders with a high raw burst count but fewer than 3 real rule
    infractions are de-weighted proportionally so that burst-only
    behaviour does not dominate the shortlist.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


# -----------------------------------------------------------------------------
# Posterior-dominance scoring
# -----------------------------------------------------------------------------

def posterior_state_dominance(
    model,
    sequences: list[np.ndarray],
    high_risk_states: list[int],
) -> np.ndarray:
    """For each sequence, return the mean posterior mass on the
    high-risk state set across all timesteps.

    Parameters
    ----------
    model : a fitted HMM exposing a predict_proba(X, lengths) method OR a
            _forward_backward(obs) -> (log_alpha, log_beta, ...) method.
            hmmlearn.CategoricalHMM satisfies the first interface; our
            fallback satisfies the second.
    sequences : list of 1-D int arrays (per-rider symbol sequences).
    high_risk_states : indices of HMM states interpreted as high-risk by
                       the trained emission matrix (chosen post-hoc from
                       the emission probabilities over fraud-shaped
                       symbols like ACTIVATE_FAST_HANDHELD and
                       ACTIVATE_GAMING_THRESHOLD).

    Returns
    -------
    np.ndarray of shape (n_riders,) with values in [0, 1].
    """
    scores = np.empty(len(sequences), dtype=float)
    high_risk_states = list(high_risk_states)

    for i, seq in enumerate(sequences):
        gamma = _posterior(model, seq)                   # shape (T, K)
        scores[i] = gamma[:, high_risk_states].sum(axis=1).mean()

    return scores


def _posterior(model, seq: np.ndarray) -> np.ndarray:
    """Best-effort extraction of smoothed posteriors for one sequence."""
    if hasattr(model, "predict_proba"):
        X = seq.reshape(-1, 1)
        return model.predict_proba(X, lengths=np.asarray([len(seq)]))
    if hasattr(model, "_forward_backward"):
        log_alpha, log_beta, _, log_ll = model._forward_backward(seq)
        log_gamma = log_alpha + log_beta - log_ll
        return np.exp(log_gamma)
    raise TypeError("Model does not expose posterior interface.")


def identify_high_risk_states(model, symbol_ids_high_risk: list[int]) -> list[int]:
    """Return the state indices whose emission distribution puts the most
    mass on the supplied 'fraud-shaped' symbol set.

    Half of the states (rounded up) are tagged high-risk; the rest
    low-risk. High-risk states are tagged post-hoc from the fitted emission matrix.
    """
    em = np.asarray(model.emissionprob_)                 # K x V
    risk_mass = em[:, symbol_ids_high_risk].sum(axis=1)
    order = np.argsort(-risk_mass)                       # most-risky first
    k_high = max(1, len(order) // 2)
    return sorted(order[:k_high].tolist())


# -----------------------------------------------------------------------------
# Burst-only de-weighting
# -----------------------------------------------------------------------------

@dataclass
class BurstDeweightConfig:
    # A rider's burst component is considered over-weighted if bursts
    # exceed this many events while fast+gaming infractions are below
    # ``min_infractions``.
    burst_threshold:   int = 50
    min_infractions:   int = 3
    deweight_factor:   float = 0.25    # multiply burst contribution by this


def deweight_burst_only(
    combined_score: np.ndarray,
    burst_counts:   np.ndarray,
    fast_counts:    np.ndarray,
    gaming_counts:  np.ndarray,
    cfg: BurstDeweightConfig = BurstDeweightConfig(),
) -> np.ndarray:
    """Return a de-weighted copy of ``combined_score``.

    Riders who trip the burst threshold but have fewer than
    ``cfg.min_infractions`` real infractions (fast + gaming) have the
    burst component of their score scaled down. Score shape is preserved
    so downstream ranking still works, but the 350-burst false positive
    is pushed out of the top-K.
    """
    infractions = fast_counts + gaming_counts
    burst_only_mask = (burst_counts >= cfg.burst_threshold) & \
                      (infractions    <  cfg.min_infractions)

    out = combined_score.astype(float).copy()
    # Assume combined_score already includes burst weight linearly.
    # Down-weight: subtract a proportional penalty.
    penalty = np.where(
        burst_only_mask,
        (1.0 - cfg.deweight_factor) * (burst_counts / 100.0),
        0.0,
    )
    out = np.maximum(out - penalty, 0.0)
    return out


# -----------------------------------------------------------------------------
# Combined scoring orchestration
# -----------------------------------------------------------------------------

def combined_anomaly_score(
    posterior_dominance:   np.ndarray,
    rule_violation_count:  np.ndarray,
    gaming_ratio:          np.ndarray,
    burst_counts:          np.ndarray,
    fast_counts:           np.ndarray,
    gaming_counts:         np.ndarray,
    w_posterior: float = 0.5,
    w_rules:     float = 0.3,
    w_gaming:    float = 0.15,
    w_burst:     float = 0.05,
) -> np.ndarray:
    """Produce the final combined score used to rank riders.

    Weights prioritise model-surfaced signal first,
    heuristic rule violations second, gaming-band behaviour third,
    burst volume as a minor tiebreaker.
    """
    # normalize each component to [0, 1] by its robust max
    def _norm(x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=float)
        p95 = np.nanpercentile(x, 95) if x.size else 1.0
        if p95 <= 0 or not np.isfinite(p95):
            return np.zeros_like(x)
        return np.clip(x / p95, 0, 1)

    raw = (
        w_posterior * _norm(posterior_dominance)
        + w_rules   * _norm(rule_violation_count)
        + w_gaming  * _norm(gaming_ratio)
        + w_burst   * _norm(burst_counts)
    )

    return deweight_burst_only(
        combined_score=raw,
        burst_counts=burst_counts,
        fast_counts=fast_counts,
        gaming_counts=gaming_counts,
    )


__all__ = [
    "posterior_state_dominance",
    "identify_high_risk_states",
    "BurstDeweightConfig",
    "deweight_burst_only",
    "combined_anomaly_score",
]
