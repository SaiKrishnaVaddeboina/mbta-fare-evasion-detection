"""
UC2 activation symbol vocabulary.

Defines seven observation symbols emitted per activation event:

  0 ACTIVATE_FAST_HANDHELD       handheld scan within 15s of activation
  1 ACTIVATE_GAMING_THRESHOLD    handheld scan 16-30s after activation
  2 ACTIVATE_SLOW_HANDHELD       handheld scan 30-300s after activation
  3 ACTIVATE_GATE                gate scan within 120s, no fast handheld
  4 NO_HANDHELD_FOLLOWUP         no handheld or gate scan in window
  5 OTHER_HANDHELD_PATTERN       fall-through catch-all
  6 PURCHASE_THEN_ACTIVATE_FAST  purchase <=60s before, handheld <=15s after

Emission rules are evaluated first-match-wins in the order above, so
inspector-triggered behaviour (PURCHASE_THEN_ACTIVATE_FAST) is tagged
distinctly from a generic fast handheld scan.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# -----------------------------------------------------------------------------
# Symbol table (8 symbols, zero-indexed for HMM)
# -----------------------------------------------------------------------------

SYMBOLS: dict[str, int] = {
    "ACTIVATE_FAST_HANDHELD":       0,
    "ACTIVATE_GAMING_THRESHOLD":    1,
    "ACTIVATE_SLOW_HANDHELD":       2,
    "ACTIVATE_GATE":                3,   # gate-validation pathway
    "NO_HANDHELD_FOLLOWUP":         4,
    "OTHER_HANDHELD_PATTERN":       5,
    "PURCHASE_THEN_ACTIVATE_FAST":  6,
}
# REPEAT_FAST_HANDHELD intentionally omitted; repeat behaviour is

SYMBOL_NAMES: dict[int, str] = {v: k for k, v in SYMBOLS.items()}
N_SYMBOLS: int = len(SYMBOLS)


# -----------------------------------------------------------------------------
# Timing thresholds (all in seconds)
# -----------------------------------------------------------------------------
# These align with the Pattern HIGH / MEDIUM rules in uc2_features.py.
# FAST covers the HIGH window; GAMING covers the band JUST above HIGH
# where riders try to dodge the rule by waiting 16-30s.

FAST_S:           int = 15            # HIGH window
GAMING_MIN_S:     int = 16
GAMING_MAX_S:     int = 30            # MEDIUM window upper bound
SLOW_MAX_S:       int = 300           # 5 min cutoff for "slow but still linked"
GATE_WINDOW_S:    int = 120           # gate taps often delayed 30-120s
PURCHASE_WINDOW_S:int = 60            # purchase-just-before-activate signal


# -----------------------------------------------------------------------------
# Per-event record structure
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class ActivationEvent:
    """Minimal per-activation record used to emit an HMM symbol."""
    account_id:              str
    activation_ts:           float             # epoch seconds
    seconds_since_purchase:  Optional[float]   # None if no recent purchase
    seconds_to_handheld:     Optional[float]   # None if no handheld follow-up
    seconds_to_gate:         Optional[float]   # None if no gate follow-up


def emit_symbol(evt: ActivationEvent) -> int:
    """Return the HMM symbol id for a single activation event.

    Applies the ordered rule list documented at the top of this module.
    """
    # Rule 1: purchase-then-activate-fast
    if (
        evt.seconds_since_purchase is not None
        and evt.seconds_since_purchase <= PURCHASE_WINDOW_S
        and evt.seconds_to_handheld is not None
        and evt.seconds_to_handheld <= FAST_S
    ):
        return SYMBOLS["PURCHASE_THEN_ACTIVATE_FAST"]

    # Rule 2: gaming-threshold (just above FAST, within GAMING band)
    if (
        evt.seconds_to_handheld is not None
        and GAMING_MIN_S <= evt.seconds_to_handheld <= GAMING_MAX_S
    ):
        return SYMBOLS["ACTIVATE_GAMING_THRESHOLD"]

    # Rule 3: fast handheld
    if (
        evt.seconds_to_handheld is not None
        and evt.seconds_to_handheld <= FAST_S
    ):
        return SYMBOLS["ACTIVATE_FAST_HANDHELD"]

    # Rule 4: gate scan (NEW)
    if (
        evt.seconds_to_gate is not None
        and evt.seconds_to_gate <= GATE_WINDOW_S
    ):
        return SYMBOLS["ACTIVATE_GATE"]

    # Rule 5: slow handheld
    if (
        evt.seconds_to_handheld is not None
        and evt.seconds_to_handheld <= SLOW_MAX_S
    ):
        return SYMBOLS["ACTIVATE_SLOW_HANDHELD"]

    # Rule 6: no follow-up at all
    if evt.seconds_to_handheld is None and evt.seconds_to_gate is None:
        return SYMBOLS["NO_HANDHELD_FOLLOWUP"]

    # Rule 7: fall-through
    return SYMBOLS["OTHER_HANDHELD_PATTERN"]


__all__ = [
    "SYMBOLS",
    "SYMBOL_NAMES",
    "N_SYMBOLS",
    "FAST_S",
    "GAMING_MIN_S",
    "GAMING_MAX_S",
    "SLOW_MAX_S",
    "GATE_WINDOW_S",
    "PURCHASE_WINDOW_S",
    "ActivationEvent",
    "emit_symbol",
]
