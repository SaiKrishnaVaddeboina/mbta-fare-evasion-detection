"""
UC2 I/O helpers

Reads the four operational CSVs exactly as they ship:

  retail_activations.csv
     canonical columns used: account_id, server_timestamp (=activation),
     ticket_id, purchase_id, purchase_timestamp (denormalised)

  retail_ticket_purchases.csv
     canonical columns used: account_id, server_timestamp (=purchase),
     purchase_id

  retail_tickets.csv
     bridge table — used only if we need ticket_id <-> account_id mapping.

  validation_scans.csv
     canonical columns used: user_id (same identifier space as account_id),
     server_timestamp (=scan), activation_timestamp (denormalised),
     ticket_id, application_name (all 'inspect-ios' or 'inspect-jvm' =
     handheld inspector devices; no gate scans in the current dataset).

Every timestamp is parsed to tz-aware UTC via ``_to_utc`` and
errors if any row fails.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Any

import pandas as pd


# -----------------------------------------------------------------------------
# Paths / environment config
# -----------------------------------------------------------------------------

@dataclass
class UC2Paths:
    base: Path = Path(".")

    activations:      str = "retail_activations.csv"
    ticket_purchases: str = "retail_ticket_purchases.csv"
    tickets:          str = "retail_tickets.csv"
    validation_scans: str = "validation_scans.csv"

    stops:            str = "commuter_rail_stops.csv"
    calendar:         str = "calendar_of_events.csv"
    boardings:        str = "commuter_rail_boardings_by_line.csv"

    out_dir: Path = field(default_factory=lambda: Path("outputs"))

    def p(self, name: str) -> Path:
        return self.base / getattr(self, name)


# -----------------------------------------------------------------------------
# Timestamp / backend helpers
# -----------------------------------------------------------------------------

_MAX_BAD_TIMESTAMP_FRAC = 0.001   # accept up to 0.1% unparseable timestamps


def _to_utc(series: pd.Series, *, strict: bool = True,
            name: str = "timestamp") -> pd.Series:
    """Parse to tz-aware UTC.

    CSVs ship naive local timestamps in America/New_York (confirmed
    from the ``timezone`` column = 'America/New_York'). We localise first
    then convert to UTC so all downstream maths is in UTC.

    Strict mode originally raised on ANY unparseable row (fine for the 200k
    sample), but the full 6.4 M-row production dumps have a small tail of
    malformed rows (~0.003 %). We now:
      * always coerce bad rows to NaT (never raise at parse time);
      * in strict mode, raise only if >0.1 % of rows are NaT — that bar
        catches schema-change disasters without tripping on noise;
      * log a warning with the dropped-row count either way.
    The caller is responsible for dropna-ing rows where a primary timestamp
    is required (readers do this for activation_ts / scan_ts / purchase_ts).
    """
    import warnings
    s = pd.to_datetime(series, errors="coerce")
    if s.dt.tz is None:
        s = s.dt.tz_localize("America/New_York", nonexistent="shift_forward",
                             ambiguous="NaT").dt.tz_convert("UTC")
    else:
        s = s.dt.tz_convert("UTC")
    bad = int(s.isna().sum())
    if bad:
        frac = bad / max(len(s), 1)
        msg = (f"_to_utc({name}): {bad:,} / {len(s):,} rows "
               f"({frac*100:.3f}%) failed to parse — coerced to NaT")
        if strict and frac > _MAX_BAD_TIMESTAMP_FRAC:
            raise ValueError(msg + f" (exceeds {_MAX_BAD_TIMESTAMP_FRAC*100:.1f}% threshold)")
        warnings.warn(msg, stacklevel=2)
    return s


def _read_csv_dispatch(paths: UC2Paths, attr: str,
                       usecols: Optional[list[str]] = None,
                       nrows: Optional[int] = None,
                       chunksize: Optional[int] = None,
                       dtype: Optional[dict] = None,
                       date_filter: Optional[tuple] = None,
                       dropna_subset: Optional[list[str]] = None) -> pd.DataFrame:
    """Unified CSV reader.

    For memory-constrained laptops on the full 5.5 GB activations CSV, pass
    ``chunksize`` (e.g. 1_000_000) to stream in chunks. Combined with a
    ``dtype`` map that marks repeated-string columns as ``category``, this
    cuts peak RAM for the 50M-row activations table from ~20 GB (string
    objects) to ~3 GB (categorical codes).

    Chunked reads use ``pd.api.types.union_categoricals`` to merge per-chunk
    category sets into a single unioned dictionary — otherwise ``pd.concat``
    on chunks with disjoint categories silently downcasts back to object,
    defeating the memory win.
    """
    kwargs: dict[str, Any] = {"low_memory": False}
    if usecols is not None:
        kwargs["usecols"] = usecols
    if nrows is not None:
        kwargs["nrows"] = nrows
    if dtype is not None:
        kwargs["dtype"] = dtype
    if chunksize is None:
        return pd.read_csv(paths.p(attr), **kwargs)
    kwargs["chunksize"] = chunksize

    cat_cols: list[str] = (
        [c for c, t in dtype.items() if str(t) == "category"]
        if dtype else []
    )

    parts: list[pd.DataFrame] = []
    total = 0
    for chunk in pd.read_csv(paths.p(attr), **kwargs):
        # Optional per-chunk date filter. Pass
        # ``date_filter=(col_name, '2025-07-01', '2025-08-01')`` to keep
        # only rows whose timestamp column falls in the half-open
        # [lo, hi) window.
        if date_filter is not None:
            col, lo, hi = date_filter
            ts = pd.to_datetime(chunk[col], errors="coerce")
            mask = pd.Series(True, index=chunk.index)
            if lo is not None:
                mask &= ts >= pd.Timestamp(lo)
            if hi is not None:
                mask &= ts < pd.Timestamp(hi)
            chunk = chunk[mask]
        # Optional per-chunk dropna. Pushing this down from read_*() keeps
        # the transient concat surface smaller — on activations that's a
        # ~40 % reduction because anonymous/guest rows drop here before
        # ever entering ``parts``.
        if dropna_subset is not None and len(chunk):
            chunk = chunk.dropna(subset=dropna_subset)
        if len(chunk):
            parts.append(chunk)
        total += len(chunk)
        if nrows is not None and total >= nrows:
            break
    if not parts:
        return pd.DataFrame(columns=usecols or [])

    # Union categoricals column-by-column so the merged frame keeps the
    # category dtype (big memory win on hash-like ID columns).
    if cat_cols and len(parts) > 1:
        from pandas.api.types import union_categoricals
        result: dict[str, Any] = {}
        for col in parts[0].columns:
            series_list = [p[col] for p in parts]
            if col in cat_cols and all(
                isinstance(s.dtype, pd.CategoricalDtype) for s in series_list
            ):
                result[col] = union_categoricals(series_list, sort_categories=False)
            else:
                result[col] = pd.concat(series_list, ignore_index=True, copy=False)
        df = pd.DataFrame(result)
    else:
        df = pd.concat(parts, ignore_index=True, copy=False)

    if nrows is not None:
        df = df.iloc[:nrows]
    return df


# -----------------------------------------------------------------------------
# Readers (source schema)
# -----------------------------------------------------------------------------

ACTIVATION_COLS = [
    "account_id", "server_timestamp", "ticket_id",
    "purchase_id", "purchase_timestamp",
]

PURCHASE_COLS = [
    "account_id", "server_timestamp", "purchase_id",
]

SCAN_COLS = [
    "user_id", "server_timestamp", "activation_timestamp",
    "ticket_id", "application_name", "action_name",
]

TICKET_COLS = [
    "ticket_id", "account_id", "purchase_id",
    "product_fare_type", "product_name",
]


CHUNKSIZE_BIG: int = 1_000_000   # rows per chunk on the big CSVs


def read_activations(paths: UC2Paths,
                     nrows: Optional[int] = None,
                     chunksize: Optional[int] = CHUNKSIZE_BIG,
                     date_from: Optional[str] = None,
                     date_to: Optional[str] = None) -> pd.DataFrame:
    """Activations — returns columns:
        account_id, activation_ts (UTC), ticket_id, purchase_id,
        purchase_ts (UTC, denormalised from ticket purchase)
    Rows with null account_id are dropped. Chunked read + categorical
    strings keep peak RAM under ~4 GB on the full 5.5 GB CSV.

    ``date_from`` / ``date_to`` (inclusive/exclusive naive ISO strings,
    e.g. '2025-07-01' / '2025-08-01') filter rows at chunk-read time
    BEFORE concat so month-by-month processing on 16 GB RAM stays safe.
    """
    dtype = {"account_id": "category", "ticket_id": "category",
             "purchase_id": "category"}
    date_filter = ("server_timestamp", date_from, date_to) if (date_from or date_to) else None
    df = _read_csv_dispatch(paths, "activations", ACTIVATION_COLS,
                            nrows=nrows, chunksize=chunksize, dtype=dtype,
                            date_filter=date_filter,
                            dropna_subset=["account_id"])
    df["activation_ts"] = _to_utc(df.pop("server_timestamp"), name="activation_ts")
    df["purchase_ts"]   = _to_utc(df.pop("purchase_timestamp"), strict=False, name="purchase_ts")
    # drop the handful of rows where the PRIMARY timestamp failed to parse
    before = len(df)
    df = df[df["activation_ts"].notna()]
    if len(df) < before:
        import warnings
        warnings.warn(
            f"read_activations: dropped {before-len(df):,} rows with NaT activation_ts",
            stacklevel=2,
        )
    return df


def read_purchases(paths: UC2Paths,
                   nrows: Optional[int] = None,
                   chunksize: Optional[int] = CHUNKSIZE_BIG,
                   date_from: Optional[str] = None,
                   date_to: Optional[str] = None) -> pd.DataFrame:
    """Purchase events — account_id, purchase_ts (UTC), purchase_id."""
    dtype = {"account_id": "category", "purchase_id": "category"}
    date_filter = ("server_timestamp", date_from, date_to) if (date_from or date_to) else None
    df = _read_csv_dispatch(paths, "ticket_purchases", PURCHASE_COLS,
                            nrows=nrows, chunksize=chunksize, dtype=dtype,
                            date_filter=date_filter,
                            dropna_subset=["account_id"])
    df["purchase_ts"] = _to_utc(df.pop("server_timestamp"), name="purchase_ts")
    before = len(df)
    df = df[df["purchase_ts"].notna()]
    if len(df) < before:
        import warnings
        warnings.warn(
            f"read_purchases: dropped {before-len(df):,} rows with NaT purchase_ts",
            stacklevel=2,
        )
    return df


def read_scans(paths: UC2Paths,
               nrows: Optional[int] = None,
               chunksize: Optional[int] = CHUNKSIZE_BIG,
               date_from: Optional[str] = None,
               date_to: Optional[str] = None) -> pd.DataFrame:
    """Validation scans — returns:
        account_id (= user_id), scan_ts (UTC), activation_ts (UTC,
        denormalised from the ticket being validated), ticket_id,
        scan_source in {'handheld', 'gate'} (currently all handheld).
    """
    dtype = {"user_id": "category", "ticket_id": "category",
             "application_name": "category", "action_name": "category"}
    date_filter = ("server_timestamp", date_from, date_to) if (date_from or date_to) else None
    df = _read_csv_dispatch(paths, "validation_scans", SCAN_COLS,
                            nrows=nrows, chunksize=chunksize, dtype=dtype,
                            date_filter=date_filter,
                            dropna_subset=["user_id"])
    df = df.rename(columns={"user_id": "account_id"})
    df["scan_ts"]       = _to_utc(df.pop("server_timestamp"), name="scan_ts")
    df["activation_ts"] = _to_utc(df.pop("activation_timestamp"), strict=False,
                                  name="activation_ts (denormalised)")
    before = len(df)
    df = df[df["scan_ts"].notna()]
    if len(df) < before:
        import warnings
        warnings.warn(
            f"read_scans: dropped {before-len(df):,} rows with NaT scan_ts",
            stacklevel=2,
        )

    # Current dataset has only inspect-ios / inspect-jvm handhelds.
    # We tag them handheld here; if a future dataset ships gate scans,
    # extend this mapping accordingly.
    df["scan_source"] = df["application_name"].astype(str).str.lower().map(
        lambda s: "gate" if "gate" in s else "handheld"
    )
    return df


def read_tickets(paths: UC2Paths, nrows: Optional[int] = None) -> pd.DataFrame:
    """Ticket reference — ticket_id <-> account_id <-> purchase_id."""
    return _read_csv_dispatch(paths, "tickets", TICKET_COLS, nrows=nrows)


# -----------------------------------------------------------------------------
# Enrichment
# -----------------------------------------------------------------------------

def read_stops(paths: UC2Paths) -> pd.DataFrame:
    return _read_csv_dispatch(paths, "stops")


def read_calendar(paths: UC2Paths) -> pd.DataFrame:
    df = _read_csv_dispatch(paths, "calendar")
    df["servicedate"] = pd.to_datetime(df["servicedate"]).dt.date
    return df


def read_boardings(paths: UC2Paths) -> pd.DataFrame:
    df = _read_csv_dispatch(paths, "boardings")
    df["servicedate"] = pd.to_datetime(df["servicedate"]).dt.date
    return df


def enrich_with_calendar(events: pd.DataFrame,
                         calendar: pd.DataFrame) -> pd.DataFrame:
    """Attach calendar_of_events flags keyed on event service date
    (UTC -> Eastern for MBTA)."""
    events = events.copy()
    events["servicedate"] = (
        events["activation_ts"]
        .dt.tz_convert("America/New_York")
        .dt.date
    )
    keep = [c for c in calendar.columns if c.startswith("has_") or c == "servicedate"]
    return events.merge(calendar[keep], on="servicedate", how="left")


# -----------------------------------------------------------------------------
# Default path resolver
# -----------------------------------------------------------------------------

def auto_paths(local_base: Optional[Path] = None) -> UC2Paths:
    """Return a UC2Paths anchored at ``local_base`` (or the cwd)."""
    return UC2Paths(base=local_base or Path("."))


__all__ = [
    "UC2Paths", "auto_paths",
    "read_activations", "read_purchases", "read_scans", "read_tickets",
    "read_stops", "read_calendar", "read_boardings",
    "enrich_with_calendar",
    "ACTIVATION_COLS", "PURCHASE_COLS", "SCAN_COLS", "TICKET_COLS",
]
