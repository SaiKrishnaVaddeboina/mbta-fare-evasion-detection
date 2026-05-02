"""
UC2 HMM training utilities.

Trains a CategoricalHMM (or a pure-numpy Baum-Welch fallback when
hmmlearn is unavailable) across a grid of state counts and random
seeds. Fits run in parallel via ProcessPoolExecutor using the 'fork'
multiprocessing context on two-thirds of the available CPU cores.
Model selection uses BIC.
"""

from __future__ import annotations

import math
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from multiprocessing import get_context
from typing import Optional

import numpy as np

try:
    from hmmlearn.hmm import CategoricalHMM as _HMMLearnCategorical
    _HAVE_HMMLEARN = True
except Exception:                          # pragma: no cover
    _HAVE_HMMLEARN = False


# -----------------------------------------------------------------------------
# Minimal fallback categorical HMM (only used if hmmlearn is missing)
# -----------------------------------------------------------------------------

class _FallbackCategoricalHMM:
    """Pure-numpy categorical HMM with Baum-Welch training."""

    def __init__(
        self,
        n_components: int,
        n_features: int,
        n_iter: int = 50,
        tol: float = 1e-3,
        random_state: Optional[int] = None,
    ):
        self.n_components = n_components
        self.n_features   = n_features
        self.n_iter       = n_iter
        self.tol          = tol
        self.random_state = random_state

    def _init_params(self) -> None:
        rng = np.random.default_rng(self.random_state)
        self.startprob_ = rng.dirichlet(np.ones(self.n_components))
        self.transmat_  = rng.dirichlet(np.ones(self.n_components),
                                        size=self.n_components)
        self.emissionprob_ = rng.dirichlet(np.ones(self.n_features),
                                           size=self.n_components)

    def _forward_backward(self, obs: np.ndarray):
        T = len(obs)
        K = self.n_components
        log_pi = np.log(self.startprob_ + 1e-300)
        log_A  = np.log(self.transmat_  + 1e-300)
        log_B  = np.log(self.emissionprob_[:, obs] + 1e-300)    # K x T

        log_alpha = np.full((T, K), -np.inf)
        log_alpha[0] = log_pi + log_B[:, 0]
        for t in range(1, T):
            log_alpha[t] = np.logaddexp.reduce(
                log_alpha[t - 1][:, None] + log_A, axis=0
            ) + log_B[:, t]

        log_beta = np.zeros((T, K))
        for t in range(T - 2, -1, -1):
            log_beta[t] = np.logaddexp.reduce(
                log_A + log_B[:, t + 1] + log_beta[t + 1], axis=1
            )

        log_ll = np.logaddexp.reduce(log_alpha[-1])
        return log_alpha, log_beta, log_B, log_ll

    def fit(self, X: np.ndarray, lengths: np.ndarray) -> "_FallbackCategoricalHMM":
        self._init_params()
        prev_ll = -np.inf
        obs_all = X.ravel()
        for _ in range(self.n_iter):
            start_accum = np.zeros(self.n_components)
            trans_accum = np.zeros((self.n_components, self.n_components))
            emit_accum  = np.zeros((self.n_components, self.n_features))
            ll_total = 0.0

            offset = 0
            for L in lengths:
                obs = obs_all[offset:offset + L]
                offset += L
                log_alpha, log_beta, log_B, log_ll = self._forward_backward(obs)
                ll_total += log_ll

                log_gamma = log_alpha + log_beta - log_ll
                gamma = np.exp(log_gamma)

                start_accum += gamma[0]
                # xi (vectorised across t)
                if L > 1:
                    log_A = np.log(self.transmat_ + 1e-300)
                    # shape: (L-1, K, K)
                    log_xi = (log_alpha[:-1, :, None]
                              + log_A[None, :, :]
                              + log_B[:, 1:].T[:, None, :]
                              + log_beta[1:, None, :] - log_ll)
                    trans_accum += np.exp(log_xi).sum(axis=0)
                # emission accum via np.add.at (vectorised)
                np.add.at(emit_accum.T, obs, gamma)   # emit_accum.T: (n_features, K)

            self.startprob_    = start_accum / start_accum.sum()
            self.transmat_     = trans_accum / trans_accum.sum(axis=1, keepdims=True)
            self.emissionprob_ = emit_accum / emit_accum.sum(axis=1, keepdims=True)

            if abs(ll_total - prev_ll) < self.tol:
                break
            prev_ll = ll_total

        self._final_ll = prev_ll
        return self

    def score(self, X: np.ndarray, lengths: np.ndarray) -> float:
        obs_all = X.ravel()
        total = 0.0
        offset = 0
        for L in lengths:
            obs = obs_all[offset:offset + L]
            offset += L
            _, _, _, ll = self._forward_backward(obs)
            total += ll
        return total


def _make_hmm(
    n_components: int,
    n_features: int,
    random_state: int,
    n_iter: int = 50,
):
    if _HAVE_HMMLEARN:
        hmm = _HMMLearnCategorical(
            n_components=n_components,
            n_features=n_features,
            n_iter=n_iter,
            tol=1e-3,
            random_state=random_state,
            init_params="ste",
            params="ste",
        )
        return hmm
    return _FallbackCategoricalHMM(
        n_components=n_components,
        n_features=n_features,
        n_iter=n_iter,
        random_state=random_state,
    )


# -----------------------------------------------------------------------------
# Training results
# -----------------------------------------------------------------------------

@dataclass
class FitResult:
    n_components: int
    seed: int
    log_likelihood: float
    n_params: int
    n_obs: int

    @property
    def bic(self) -> float:
        return -2 * self.log_likelihood + self.n_params * math.log(max(self.n_obs, 1))

    @property
    def aic(self) -> float:
        return -2 * self.log_likelihood + 2 * self.n_params


def _count_params(n_components: int, n_features: int) -> int:
    """Free parameters in a categorical HMM."""
    return (
        (n_components - 1)                          # startprob
        + n_components * (n_components - 1)         # transition rows
        + n_components * (n_features - 1)           # emission rows
    )


def _fit_one(
    n_components: int,
    n_features: int,
    seed: int,
    X: np.ndarray,
    lengths: np.ndarray,
    n_iter: int,
) -> tuple[FitResult, object]:
    hmm = _make_hmm(n_components, n_features, seed, n_iter)
    hmm.fit(X, lengths)
    ll = hmm.score(X, lengths)
    result = FitResult(
        n_components=n_components,
        seed=seed,
        log_likelihood=ll,
        n_params=_count_params(n_components, n_features),
        n_obs=int(lengths.sum()),
    )
    return result, hmm


# -----------------------------------------------------------------------------
# Parallel multi-seed / multi-state search
# -----------------------------------------------------------------------------

def train_multi(
    X: np.ndarray,
    lengths: np.ndarray,
    n_features: int,
    state_grid: tuple[int, ...] = (7, 9, 11),
    n_seeds: int = 8,
    n_iter: int = 50,
    max_workers: Optional[int] = None,
) -> dict:
    """Fit the grid of (n_components x seeds) in parallel, pick best by BIC.

    Returns
    -------
    dict with keys:
        best_model       : the fitted HMM object with lowest BIC
        best_result      : FitResult for that model
        all_results      : list[FitResult] across the full grid
    """
    if max_workers is None:
        total = os.cpu_count() or 1
        max_workers = max(1, (total * 2) // 3)   # 2/3 of available cores

    seeds = list(range(n_seeds))
    jobs = [(k, seed) for k in state_grid for seed in seeds]

    ctx = get_context("fork")                    # fork context for parallel fits
    all_results: list[FitResult] = []
    models: dict[tuple[int, int], object] = {}

    with ProcessPoolExecutor(max_workers=max_workers, mp_context=ctx) as pool:
        futures = {
            pool.submit(_fit_one, k, n_features, seed, X, lengths, n_iter): (k, seed)
            for (k, seed) in jobs
        }
        for fut in as_completed(futures):
            key = futures[fut]
            result, model = fut.result()
            all_results.append(result)
            models[key] = model

    best_result = min(all_results, key=lambda r: r.bic)
    best_model  = models[(best_result.n_components, best_result.seed)]

    return {
        "best_model":  best_model,
        "best_result": best_result,
        "all_results": all_results,
    }


__all__ = ["FitResult", "train_multi", "_count_params"]
