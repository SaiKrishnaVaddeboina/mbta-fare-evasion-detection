"""Generate synthetic CSVs matching the production schema.

Produces small dummy CSVs in the Masabi UC2 schema so the pipeline in
``pipeline/`` can be exercised end-to-end without access to the real
MBTA data. The fitted HMM and shortlists from a synthetic run are not
interpretable; the purpose is solely to verify the code path.

Usage:
    python scripts/generate_synthetic_data.py --out pipeline/data/ --riders 1000
"""

from __future__ import annotations

import argparse
import random
from pathlib import Path

import numpy as np
import pandas as pd


RNG = np.random.default_rng(0)
random.seed(0)


def _ts(start: pd.Timestamp, secs: float) -> str:
    return (start + pd.Timedelta(seconds=float(secs))).strftime("%Y-%m-%d %H:%M:%S")


def make_synthetic(out_dir: Path, n_riders: int, days: int) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    start = pd.Timestamp("2025-07-01 06:00:00")

    activations, purchases, tickets, scans = [], [], [], []

    for i in range(n_riders):
        account_id = f"acct_{i:06d}"
        # Mix of behaviours: 80% normal, 15% suspicious, 5% inspector-reactive
        roll = RNG.random()
        n_events = int(RNG.integers(5, 40))

        for k in range(n_events):
            ticket_id = f"tkt_{i:06d}_{k:03d}"
            purchase_id = f"pur_{i:06d}_{k:03d}"
            day_offset = RNG.integers(0, days * 86_400)
            purchase_t = day_offset
            activation_t = purchase_t + (
                RNG.uniform(0, 30) if roll > 0.85 else RNG.uniform(60, 7200)
            )
            # Suspicious riders scan fast
            if roll > 0.85:
                gap = RNG.uniform(0.5, 25)
            elif roll > 0.80:
                gap = RNG.uniform(16, 30)
            else:
                gap = RNG.uniform(40, 600)
            scan_t = activation_t + gap

            activations.append({
                "account_id": account_id,
                "server_timestamp": _ts(start, activation_t),
                "ticket_id": ticket_id,
                "purchase_id": purchase_id,
                "purchase_timestamp": _ts(start, purchase_t),
            })
            purchases.append({
                "account_id": account_id,
                "server_timestamp": _ts(start, purchase_t),
                "purchase_id": purchase_id,
            })
            tickets.append({
                "ticket_id": ticket_id,
                "account_id": account_id,
                "purchase_id": purchase_id,
                "product_fare_type": "single",
                "product_name": "ZONE_1A_SINGLE",
            })
            scans.append({
                "user_id": account_id,
                "server_timestamp": _ts(start, scan_t),
                "activation_timestamp": _ts(start, activation_t),
                "ticket_id": ticket_id,
                "application_name": "inspect-ios",
                "action_name": "validate",
            })

    pd.DataFrame(activations).to_csv(out_dir / "retail_activations.csv", index=False)
    pd.DataFrame(purchases).to_csv(out_dir / "retail_ticket_purchases.csv", index=False)
    pd.DataFrame(tickets).to_csv(out_dir / "retail_tickets.csv", index=False)
    pd.DataFrame(scans).to_csv(out_dir / "validation_scans.csv", index=False)

    # Calendar — one row per day with random flags
    cal = pd.DataFrame({
        "servicedate": [(start + pd.Timedelta(days=d)).date() for d in range(days)],
        "has_holiday": RNG.random(days) < 0.05,
        "has_school_day": RNG.random(days) < 0.7,
        "has_event": RNG.random(days) < 0.1,
        "has_disruption": RNG.random(days) < 0.05,
        "has_maintenance": RNG.random(days) < 0.08,
    })
    cal.to_csv(out_dir / "calendar_of_events.csv", index=False)

    # Reference tables (loaded but not joined)
    pd.DataFrame({
        "stop_id": [f"stop_{i:03d}" for i in range(20)],
        "stop_name": [f"Stop {i}" for i in range(20)],
        "stop_lat": RNG.uniform(42.2, 42.5, 20),
        "stop_lon": RNG.uniform(-71.3, -70.9, 20),
        "line_id": RNG.choice(["A", "B", "C"], 20),
    }).to_csv(out_dir / "commuter_rail_stops.csv", index=False)

    pd.DataFrame({
        "servicedate": [(start + pd.Timedelta(days=d)).date() for d in range(days)],
        "line_id": "A",
        "boardings": RNG.integers(1000, 5000, days),
    }).to_csv(out_dir / "commuter_rail_boardings_by_line.csv", index=False)

    print(f"Wrote synthetic data for {n_riders:,} riders × {days} days to {out_dir}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="pipeline/data", type=Path,
                    help="Output directory (default: pipeline/data)")
    ap.add_argument("--riders", default=1000, type=int,
                    help="Number of synthetic riders (default: 1000)")
    ap.add_argument("--days", default=120, type=int,
                    help="Span in days (default: 120)")
    args = ap.parse_args()
    make_synthetic(args.out, args.riders, args.days)


if __name__ == "__main__":
    main()
