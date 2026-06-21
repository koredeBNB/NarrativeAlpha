"""NarrativeAlpha historical price + sentiment layer (Phase 4).

The CMC narrative tool is live-only and CMC OHLCV history is gated on our plan
(see PRD sec.7), so the backtest reconstructs sector baskets from **free Binance
daily klines** and gates risk with the **Fear & Greed historical** index. This
module is the data plumbing for that:

  - :func:`load_klines`        - daily OHLCV for one Binance ``*USDT`` pair, cached.
  - :func:`load_price_panel`   - aligned ``open``/``close`` panels for many pairs.
  - :func:`load_fear_greed`    - historical crypto Fear & Greed (0-100), cached.

Everything is cached to ``.cache/`` (gitignored) so repeat backtests are fast and
fully offline after the first run. Caches are keyed by symbol/interval and are
incrementally extended, never silently truncated, so results stay deterministic.

CLI::

    python scripts/data.py BTCUSDT ETHUSDT      # warm the cache + print a preview
    python scripts/data.py --fng                # fetch the Fear & Greed series
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import pandas as pd
import requests

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CACHE_DIR = _REPO_ROOT / ".cache"
_KLINES_CACHE = _CACHE_DIR / "binance"
_FNG_CACHE = _CACHE_DIR / "fng" / "fear_greed.csv"

BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
BINANCE_MAX_LIMIT = 1000  # candles per request
_MS_PER_DAY = 86_400_000

# Free, key-less crypto Fear & Greed index (the canonical crypto F&G series).
# Used as the historical risk-off input; CMC's own F&G history is plan-gated.
ALTERNATIVE_FNG_URL = "https://api.alternative.me/fng/"

_DEFAULT_START = "2022-01-01"
_OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]


# --------------------------------------------------------------- helpers -----
def _to_ms(date: str | datetime | pd.Timestamp) -> int:
    ts = pd.Timestamp(date, tz="UTC") if not isinstance(date, pd.Timestamp) else date
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return int(ts.timestamp() * 1000)


def _today_utc_date() -> pd.Timestamp:
    return pd.Timestamp(datetime.now(timezone.utc).date())


def _kline_cache_path(pair: str, interval: str) -> Path:
    return _KLINES_CACHE / f"{pair.upper()}_{interval}.csv"


# ----------------------------------------------------- Binance klines ----
def _fetch_klines_raw(
    pair: str,
    start_ms: int,
    end_ms: int,
    interval: str,
    timeout: int,
    session: Optional[requests.Session],
) -> List[list]:
    """Page through Binance klines from ``start_ms`` to ``end_ms`` (inclusive-ish)."""
    getter = session.get if session is not None else requests.get
    rows: List[list] = []
    cursor = start_ms
    while cursor <= end_ms:
        resp = getter(
            BINANCE_KLINES_URL,
            params={
                "symbol": pair.upper(),
                "interval": interval,
                "startTime": cursor,
                "endTime": end_ms,
                "limit": BINANCE_MAX_LIMIT,
            },
            headers={"User-Agent": "narrativealpha/0.1"},
            timeout=timeout,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        rows.extend(batch)
        last_open = batch[-1][0]
        # Advance one interval past the last candle to avoid re-fetching it.
        cursor = last_open + _MS_PER_DAY
        if len(batch) < BINANCE_MAX_LIMIT:
            break
        time.sleep(0.05)  # be polite to the public endpoint
    return rows


def _raw_to_frame(rows: List[list]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=_OHLCV_COLUMNS, index=pd.DatetimeIndex([], name="date"))
    frame = pd.DataFrame(
        [
            {
                "date": pd.Timestamp(int(r[0]), unit="ms", tz="UTC").normalize().tz_localize(None),
                "open": float(r[1]),
                "high": float(r[2]),
                "low": float(r[3]),
                "close": float(r[4]),
                "volume": float(r[5]),
            }
            for r in rows
        ]
    )
    frame = frame.drop_duplicates(subset="date").set_index("date").sort_index()
    return frame[_OHLCV_COLUMNS]


def _read_cache(path: Path) -> Optional[pd.DataFrame]:
    if not path.exists():
        return None
    frame = pd.read_csv(path, parse_dates=["date"], index_col="date")
    return frame[_OHLCV_COLUMNS].sort_index()


def _write_cache(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index_label="date")


def load_klines(
    pair: str,
    start: str | datetime | pd.Timestamp = _DEFAULT_START,
    end: Optional[str | datetime | pd.Timestamp] = None,
    interval: str = "1d",
    session: Optional[requests.Session] = None,
    timeout: int = 30,
    use_cache: bool = True,
) -> pd.DataFrame:
    """Return daily OHLCV for one Binance ``*USDT`` pair, cached on disk.

    The on-disk cache (``.cache/binance/<PAIR>_<interval>.csv``) is extended
    incrementally: existing candles are reused and only the missing tail (and,
    if needed, a missing head) is fetched. The returned frame is indexed by
    naive UTC ``date`` and sliced to ``[start, end]``.
    """
    pair = pair.upper()
    end_ts = _today_utc_date() if end is None else pd.Timestamp(end).normalize()
    start_ts = pd.Timestamp(start).normalize()

    path = _kline_cache_path(pair, interval)
    cached = _read_cache(path) if use_cache else None

    # Fetch whatever the cache is missing: the head (before its earliest candle)
    # and/or the tail (after its latest). Backfilling the head matters because a
    # later-starting cache must not silently truncate an earlier requested start.
    fetch_ranges: List[Tuple[pd.Timestamp, pd.Timestamp]] = []
    if cached is None or cached.empty:
        fetch_ranges.append((start_ts, end_ts))
    else:
        cached_first, cached_last = cached.index.min(), cached.index.max()
        if start_ts < cached_first:
            fetch_ranges.append((start_ts, cached_first - pd.Timedelta(days=1)))
        if end_ts > cached_last:
            fetch_ranges.append((cached_last + pd.Timedelta(days=1), end_ts))

    fetched_parts: List[pd.DataFrame] = []
    for seg_start, seg_end in fetch_ranges:
        if seg_start > seg_end:
            continue
        raw = _fetch_klines_raw(
            pair,
            _to_ms(seg_start),
            _to_ms(seg_end) + _MS_PER_DAY - 1,
            interval,
            timeout,
            session,
        )
        part = _raw_to_frame(raw)
        if not part.empty:
            fetched_parts.append(part)

    parts = [p for p in ([cached] if cached is not None else []) + fetched_parts if p is not None and not p.empty]
    if parts:
        frame = pd.concat(parts)
        frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    else:
        frame = _raw_to_frame([])

    if use_cache and not frame.empty:
        _write_cache(path, frame)

    return frame.loc[(frame.index >= start_ts) & (frame.index <= end_ts)]


def load_price_panel(
    pairs: Iterable[str],
    start: str | datetime | pd.Timestamp = _DEFAULT_START,
    end: Optional[str | datetime | pd.Timestamp] = None,
    interval: str = "1d",
    timeout: int = 30,
    use_cache: bool = True,
    verbose: bool = False,
) -> Tuple[pd.DataFrame, pd.DataFrame, List[str]]:
    """Load aligned ``open`` and ``close`` panels for many pairs.

    Returns ``(opens, closes, missing)`` where ``opens``/``closes`` are wide
    DataFrames (rows = union of all trading dates, columns = pair) and
    ``missing`` lists pairs that returned no data on Binance (delisted, never
    listed, or unsupported). Missing/short series are kept as ``NaN`` columns so
    the caller can decide how to handle them per-date.
    """
    pairs = list(dict.fromkeys(p.upper() for p in pairs))  # de-dupe, keep order
    session = requests.Session()
    opens: Dict[str, pd.Series] = {}
    closes: Dict[str, pd.Series] = {}
    missing: List[str] = []

    for i, pair in enumerate(pairs, start=1):
        try:
            frame = load_klines(
                pair, start, end, interval, session=session,
                timeout=timeout, use_cache=use_cache,
            )
        except requests.RequestException as exc:
            if verbose:
                print(f"  [{i}/{len(pairs)}] {pair}: request failed ({exc})", file=sys.stderr)
            missing.append(pair)
            continue
        if frame.empty:
            missing.append(pair)
            if verbose:
                print(f"  [{i}/{len(pairs)}] {pair}: no data", file=sys.stderr)
            continue
        opens[pair] = frame["open"]
        closes[pair] = frame["close"]
        if verbose:
            print(f"  [{i}/{len(pairs)}] {pair}: {len(frame)} rows "
                  f"({frame.index.min().date()} -> {frame.index.max().date()})")

    open_panel = pd.DataFrame(opens).sort_index() if opens else pd.DataFrame()
    close_panel = pd.DataFrame(closes).sort_index() if closes else pd.DataFrame()
    return open_panel, close_panel, missing


# ------------------------------------------------------- Fear & Greed ----
def _fetch_alternative_fng(timeout: int) -> pd.Series:
    resp = requests.get(
        ALTERNATIVE_FNG_URL,
        params={"limit": 0, "format": "json"},  # limit=0 -> full history
        headers={"User-Agent": "narrativealpha/0.1"},
        timeout=timeout,
    )
    resp.raise_for_status()
    payload = resp.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    if not data:
        raise RuntimeError("alternative.me Fear & Greed returned no data")
    records = {
        pd.Timestamp(int(item["timestamp"]), unit="s", tz="UTC").normalize().tz_localize(None):
        float(item["value"])
        for item in data
    }
    series = pd.Series(records, name="fear_greed").sort_index()
    series.index.name = "date"
    return series


def load_fear_greed(
    start: str | datetime | pd.Timestamp = _DEFAULT_START,
    end: Optional[str | datetime | pd.Timestamp] = None,
    timeout: int = 30,
    use_cache: bool = True,
) -> pd.Series:
    """Return the historical crypto Fear & Greed index (0-100), daily, cached.

    Tries the on-disk cache first and only re-fetches when the cache does not
    cover ``end``. The series is daily and reindexed/forward-filled by the
    caller as needed.
    """
    end_ts = _today_utc_date() if end is None else pd.Timestamp(end).normalize()
    start_ts = pd.Timestamp(start).normalize()

    cached: Optional[pd.Series] = None
    if use_cache and _FNG_CACHE.exists():
        frame = pd.read_csv(_FNG_CACHE, parse_dates=["date"], index_col="date")
        cached = frame["fear_greed"].sort_index()

    need_fetch = cached is None or cached.empty or cached.index.max() < end_ts
    if need_fetch:
        fetched = _fetch_alternative_fng(timeout)
        series = fetched if cached is None else fetched.combine_first(cached).sort_index()
        if use_cache:
            _FNG_CACHE.parent.mkdir(parents=True, exist_ok=True)
            series.to_frame("fear_greed").to_csv(_FNG_CACHE, index_label="date")
    else:
        series = cached

    return series.loc[(series.index >= start_ts) & (series.index <= end_ts)]


# --------------------------------------------------------------- CLI -----
def _main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Warm the NarrativeAlpha price/sentiment cache.")
    parser.add_argument("pairs", nargs="*", help="Binance *USDT pairs, e.g. BTCUSDT ETHUSDT")
    parser.add_argument("--start", default=_DEFAULT_START)
    parser.add_argument("--end", default=None)
    parser.add_argument("--fng", action="store_true", help="Also fetch Fear & Greed history.")
    args = parser.parse_args(argv)

    if args.fng or not args.pairs:
        fng = load_fear_greed(args.start, args.end)
        print(f"Fear & Greed: {len(fng)} days "
              f"({fng.index.min().date()} -> {fng.index.max().date()}), "
              f"latest={fng.iloc[-1]:.0f}")

    if args.pairs:
        opens, closes, missing = load_price_panel(
            args.pairs, args.start, args.end, verbose=True
        )
        print(f"\nLoaded {closes.shape[1]} pairs x {closes.shape[0]} days.")
        if missing:
            print(f"Missing (no Binance data): {', '.join(missing)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
