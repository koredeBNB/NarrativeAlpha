"""NarrativeAlpha signal layer + strategy-spec builder (Phases 1 & 3).

Two responsibilities live here, by design (see PRD sec.6 / phases.md):

1. **Signal layer (Phase 1).** Turn the live ``trending_crypto_narratives``
   payload from CMC Agent Hub into a clean, ranked table of narratives - the
   core, CMC-exclusive edge (PRD sec.4.1): rank narratives by their
   *volume-weighted price performance relative to the total market*, confirm
   with absolute sector momentum, and down-rank late-stage blow-offs via an
   exhaustion filter.

2. **Strategy-spec builder (Phase 3).** Combine the live signal (Phase 1) with
   the frozen narrative->token selection map (Phase 2) and default timing / risk
   / cost parameters into a typed ``strategy_spec.json``, validated against
   ``reference/strategy_schema.json`` with ``jsonschema``.

Public surface used by later phases:

  - :class:`SignalParams`        - tunable narrative-scoring knobs.
  - :func:`build_signal_table`   - narrative records -> ranked ``DataFrame``.
  - :func:`rank_narratives`      - fetch (via :class:`CMCClient`) + rank in one call.
  - :class:`SpecParams`          - default timing / risk / cost / sizing params.
  - :func:`build_strategy_spec`  - signal + selection + defaults -> spec dict.
  - :func:`validate_spec`        - schema-validate a spec dict (jsonschema).

Run ``python scripts/build_spec.py`` to build, validate, and write a
schema-valid ``strategy_spec.json`` naming the current top narratives and their
eligible tokens (Phase 3 acceptance check).
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pandas as pd
from jsonschema import Draft202012Validator

try:  # Allow both ``python scripts/build_spec.py`` and ``import``.
    from cmc_client import CMCClient
    from universe import (
        ELIGIBLE_TOKENS_PATH,
        NARRATIVE_MAP_PATH,
        build_narrative_token_map,
        load_eligible_tokens,
        load_narrative_token_map,
        write_narrative_token_map,
    )
except ImportError:  # pragma: no cover - import path when used as a package
    from scripts.cmc_client import CMCClient
    from scripts.universe import (
        ELIGIBLE_TOKENS_PATH,
        NARRATIVE_MAP_PATH,
        build_narrative_token_map,
        load_eligible_tokens,
        load_narrative_token_map,
        write_narrative_token_map,
    )


_REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = _REPO_ROOT / "reference" / "strategy_schema.json"
DEFAULT_SPEC_PATH = _REPO_ROOT / "strategy_spec.json"
EXAMPLE_SPEC_PATH = _REPO_ROOT / "examples" / "strategy_spec.example.json"


# Narrative-level fields we read from each parsed row (see cmc_client.parse_table).
_F_NAME = "categoryName"
_F_SLUG = "slug"
_F_VW_24H = "volumeWeightedPricePerfVsCryptoMarketCap24h"
_F_VW_7D = "volumeWeightedPricePerfVsCryptoMarketCap7d"
_F_VW_30D = "volumeWeightedPricePerfVsCryptoMarketCap30d"
_F_MC_7D = "marketCapChangePercentage7d"
_F_MC_30D = "marketCapChangePercentage30d"
_F_VOL_24H = "volume24h"
_F_AUTHORS = "socialKeywordUniqueAuthorCount"
_F_COINS = "topCoinList"
_F_COIN_SYM = "coinSymbol"


@dataclass(frozen=True)
class SignalParams:
    """Tunable knobs for narrative ranking.

    Weights apply to *fractional* inputs (e.g. ``0.0059`` for +0.59%) so the raw
    score is directly interpretable as a blended relative-strength number.
    """

    # Ranking blend: relative strength (primary) + absolute momentum (confirm).
    weight_perf_7d: float = 0.50
    weight_perf_30d: float = 0.30
    weight_confirm_mc_7d: float = 0.20

    # Exhaustion filter: a narrative is a likely late-stage blow-off when its
    # very-recent relative perf rolls over hard while its 30d perf is stretched.
    exhaustion_perf_24h_below: float = -0.02  # 24h vw-perf sharply negative
    exhaustion_perf_30d_above: float = 0.10   # 30d vw-perf very high
    exhaustion_penalty: float = 0.50          # fraction of score removed when flagged

    # Output columns required by the Phase 1 acceptance bar, in display order.
    output_columns: Sequence[str] = field(
        default=(
            "narrative",
            "score",
            "perf_7d",
            "perf_30d",
            "social_authors",
            "top_coins",
        )
    )


def _num(value: Any, default: float = 0.0) -> float:
    """Best-effort float coercion (values are pre-normalized by parse_table)."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _top_coin_symbols(record: Dict[str, Any]) -> List[str]:
    coins = record.get(_F_COINS) or []
    symbols: List[str] = []
    for coin in coins:
        if isinstance(coin, dict):
            sym = coin.get(_F_COIN_SYM)
            if sym:
                symbols.append(str(sym).strip())
    return symbols


def _is_exhausted(perf_24h: float, perf_30d: float, params: SignalParams) -> bool:
    """Late-stage blow-off: recent rollover after a stretched 30d run."""
    return (
        perf_24h < params.exhaustion_perf_24h_below
        and perf_30d > params.exhaustion_perf_30d_above
    )


def _raw_score(perf_7d: float, perf_30d: float, mc_7d: float, params: SignalParams) -> float:
    return (
        params.weight_perf_7d * perf_7d
        + params.weight_perf_30d * perf_30d
        + params.weight_confirm_mc_7d * mc_7d
    )


def build_signal_table(
    records: List[Dict[str, Any]],
    params: Optional[SignalParams] = None,
) -> pd.DataFrame:
    """Rank parsed narrative records into a scored ``DataFrame``.

    Parameters
    ----------
    records:
        Output of ``CMCClient.call("trending_crypto_narratives")`` - a list of
        per-narrative dicts with an expanded ``topCoinList``.
    params:
        Scoring configuration; defaults follow the PRD.

    Returns
    -------
    DataFrame sorted by descending ``score`` with (at least) the columns:
    ``narrative, score, perf_7d, perf_30d, social_authors, top_coins`` plus
    diagnostic columns (``slug, perf_24h, mc_7d, raw_score, exhausted``).
    """
    params = params or SignalParams()

    rows: List[Dict[str, Any]] = []
    for record in records:
        perf_24h = _num(record.get(_F_VW_24H))
        perf_7d = _num(record.get(_F_VW_7D))
        perf_30d = _num(record.get(_F_VW_30D))
        mc_7d = _num(record.get(_F_MC_7D))

        raw = _raw_score(perf_7d, perf_30d, mc_7d, params)
        exhausted = _is_exhausted(perf_24h, perf_30d, params)
        # Penalty scales toward zero so a flagged blow-off cannot out-rank a
        # healthy narrative of similar raw strength.
        score = raw * (1.0 - params.exhaustion_penalty) if exhausted else raw

        rows.append(
            {
                "narrative": str(record.get(_F_NAME, "")).strip(),
                "slug": record.get(_F_SLUG),
                "score": score,
                "raw_score": raw,
                "perf_7d": perf_7d,
                "perf_30d": perf_30d,
                "perf_24h": perf_24h,
                "mc_7d": mc_7d,
                "volume_24h": _num(record.get(_F_VOL_24H)),
                "social_authors": _num(record.get(_F_AUTHORS)),
                "exhausted": exhausted,
                "top_coins": _top_coin_symbols(record),
            }
        )

    columns = [
        "narrative", "slug", "score", "raw_score", "perf_7d", "perf_30d",
        "perf_24h", "mc_7d", "volume_24h", "social_authors", "exhausted",
        "top_coins",
    ]
    df = pd.DataFrame(rows, columns=columns)
    if not df.empty:
        df = df.sort_values("score", ascending=False, kind="mergesort").reset_index(drop=True)
        df.insert(0, "rank", df.index + 1)
    return df


def rank_narratives(
    client: Optional[CMCClient] = None,
    params: Optional[SignalParams] = None,
) -> pd.DataFrame:
    """Fetch live narratives and return the ranked signal table."""
    client = client or CMCClient()
    records = client.call("trending_crypto_narratives")
    if not isinstance(records, list):
        raise TypeError(
            f"Expected a list of narrative records, got {type(records).__name__}"
        )
    return build_signal_table(records, params)


def _fmt_pct(value: Optional[float]) -> str:
    if value is None:
        return "  n/a"
    return f"{value * 100:+.2f}%"


# ============================================================================ #
# Phase 3 - strategy-spec builder                                              #
# ============================================================================ #


@dataclass(frozen=True)
class SpecParams:
    """Default timing / risk / cost / sizing parameters for the spec (PRD sec.4.3, sec.9)."""

    name: str = "NarrativeAlpha v1"
    version: str = "1.0.0"

    # Selection engine: "narrative_rotation" (rotate into CMC narrative leaders)
    # or "cross_sectional_momentum" (universe-wide momentum, tilted toward the
    # leading CMC narratives -- the recommended, narrative-fed strategy).
    method: str = "narrative_rotation"

    # How many leading narratives to hold (rotation) / feed into the tilt (momentum).
    top_n_narratives: int = 3

    # --- cross_sectional_momentum params (used when method == that) ---
    momentum_lookbacks_days: Sequence[int] = (20, 40, 60)
    momentum_skip_days: int = 7
    momentum_top_n: int = 10
    momentum_risk_adjusted: bool = True
    narrative_tilt: float = 0.15            # CMC narrative -> momentum-score boost
    vol_target: float = 0.40               # annualized portfolio vol target
    vol_lookback_days: int = 30

    # Cadence.
    rebalance_frequency_days: int = 14
    fill: str = "next_open"

    # Selection / sizing.
    selection_weight: str = "equal"
    sizing_scheme: str = "equal_weight"
    max_weight: float = 0.15
    top_k_per_narrative: int = 8   # hold each narrative's strongest few names
    no_trade_band: float = 0.03    # skip trades smaller than this to curb churn

    # Entry timing (overbought skip).
    timing_tool: str = "get_crypto_technical_analysis"
    skip_if_rsi_above: float = 80.0

    # Risk-off gate: slow trend filter (BTC vs long MA, with hysteresis) is the
    # primary on/off switch; Fear & Greed is a true-extreme override only.
    risk_off_tool: str = "get_global_metrics_latest"
    fear_greed_min: float = 25.0
    fear_greed_extreme: float = 12.0
    trend_ma_days: int = 200
    trend_band: float = 0.03

    # Drawdown gate.
    max_drawdown: float = 0.25
    on_breach: str = "flatten_until_risk_off_clears"
    halt_reentry_mode: str = "regime_reset"  # re-enter only after the trend regime resets
    halt_cooldown_days: int = 30             # used only when halt_reentry_mode == "cooldown"

    # Costs (per unit turnover).
    fee_bps: float = 10.0
    slippage_bps: float = 5.0


def _signal_lookup(df: pd.DataFrame) -> Dict[str, Dict[str, Any]]:
    """Index the ranked signal table by slug and by lowercased narrative name."""
    lookup: Dict[str, Dict[str, Any]] = {}
    for row in df.to_dict(orient="records"):
        slug = row.get("slug")
        name = str(row.get("narrative", "")).strip().lower()
        if slug:
            lookup[f"slug::{slug}"] = row
        if name:
            lookup.setdefault(f"name::{name}", row)
    return lookup


def _match_signal(
    narrative: Dict[str, Any], lookup: Dict[str, Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    slug = narrative.get("slug")
    if slug and f"slug::{slug}" in lookup:
        return lookup[f"slug::{slug}"]
    name = str(narrative.get("narrative", "")).strip().lower()
    return lookup.get(f"name::{name}")


def _as_int(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _token_weights(num_narratives: int, num_tokens: int, max_weight: float) -> float:
    """Portfolio-level equal weight: split equally across held narratives, then
    equally across each narrative's eligible constituents, capped per token."""
    if num_narratives <= 0 or num_tokens <= 0:
        return 0.0
    raw = (1.0 / num_narratives) * (1.0 / num_tokens)
    return round(min(raw, max_weight), 6)


def build_strategy_spec(
    signal_df: pd.DataFrame,
    narrative_map: Dict[str, Any],
    signal_params: Optional[SignalParams] = None,
    spec_params: Optional[SpecParams] = None,
    universe_ref: str = "reference/eligible_tokens.json",
    map_ref: str = "reference/narrative_token_map.json",
) -> Dict[str, Any]:
    """Assemble a typed strategy spec from the live signal + frozen selection map.

    The ``selected_narratives`` are produced by ranking the *frozen* narrative
    map (Phase 2, what the backtest can actually price) by the *current live*
    score from the signal table (Phase 1), then taking the top ``top_n``
    narratives that carry at least one eligible token. This keeps the spec and
    the backtest aligned while still naming the current leaders.
    """
    signal_params = signal_params or SignalParams()
    spec_params = spec_params or SpecParams()

    lookup = _signal_lookup(signal_df)
    map_narratives = narrative_map.get("narratives", [])

    # Attach the live signal to each frozen narrative; rank by live score.
    enriched: List[Dict[str, Any]] = []
    for nar in map_narratives:
        sig = _match_signal(nar, lookup)
        if sig is None:
            continue  # stale narrative no longer trending; not currently rankable
        tokens = nar.get("tokens", [])
        if not tokens:
            continue
        enriched.append({"map": nar, "signal": sig})

    enriched.sort(key=lambda e: e["signal"].get("score", float("-inf")), reverse=True)
    held = enriched[: spec_params.top_n_narratives]
    num_held = len(held)

    selected: List[Dict[str, Any]] = []
    for idx, entry in enumerate(held, start=1):
        nar, sig = entry["map"], entry["signal"]
        tokens = nar.get("tokens", [])
        weight = _token_weights(num_held, len(tokens), spec_params.max_weight)
        token_entries = [
            {
                "symbol": tok["symbol"],
                "cmc_id": _as_int(tok.get("cmc_id")),
                "binance_pair": tok["binance_pair"],
                "target_weight": weight,
            }
            for tok in tokens
            if _as_int(tok.get("cmc_id")) is not None
        ]
        if not token_entries:
            continue
        selected.append(
            {
                "rank": idx,
                "narrative": nar.get("narrative", ""),
                "slug": nar.get("slug"),
                "score": round(float(sig.get("score", 0.0)), 6),
                "perf_7d": _round_opt(sig.get("perf_7d")),
                "perf_30d": _round_opt(sig.get("perf_30d")),
                "exhausted": bool(sig.get("exhausted", False)),
                "social_authors": _round_opt(sig.get("social_authors"), 0),
                "tokens": token_entries,
            }
        )

    spec: Dict[str, Any] = {
        "name": spec_params.name,
        "version": spec_params.version,
        "description": (
            "Long/flat, weekly-rebalanced rotation into the strongest CoinMarketCap "
            "narratives, expressed through the eligible BEP-20 universe."
        ),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "universe": {
            "source": "bep20-149",
            "ref": universe_ref,
            "size": narrative_map.get("_meta", {}).get("eligible_token_count"),
        },
        "rebalance": {
            "frequency_days": spec_params.rebalance_frequency_days,
            "fill": spec_params.fill,
        },
        "signal": {
            "source": "trending_crypto_narratives",
            "rank_by": [_F_VW_7D, _F_VW_30D],
            "confirm_by": [_F_MC_7D],
            "weights": {
                "perf_7d": signal_params.weight_perf_7d,
                "perf_30d": signal_params.weight_perf_30d,
                "confirm_mc_7d": signal_params.weight_confirm_mc_7d,
            },
            "exhaustion_filter": {
                "rule": "down_rank if perf_24h < perf_24h_below and perf_30d > perf_30d_above",
                "perf_24h_below": signal_params.exhaustion_perf_24h_below,
                "perf_30d_above": signal_params.exhaustion_perf_30d_above,
                "penalty": signal_params.exhaustion_penalty,
            },
            "top_n_narratives": spec_params.top_n_narratives,
        },
        "selection": {
            "from": "topCoinList",
            "filter": "eligible_bep20",
            "weight": spec_params.selection_weight,
        },
        "timing": {
            "tool": spec_params.timing_tool,
            "skip_if_rsi_above": spec_params.skip_if_rsi_above,
        },
        "risk_off": {
            "tool": spec_params.risk_off_tool,
            "to_cash_if": "btc < ma(trend_ma_days) - trend_band || fear_greed <= fear_greed_extreme",
            "fear_greed_min": spec_params.fear_greed_min,
            "fear_greed_extreme": spec_params.fear_greed_extreme,
            "trend_ma_days": spec_params.trend_ma_days,
            "trend_band": spec_params.trend_band,
        },
        "risk": {
            "max_drawdown": spec_params.max_drawdown,
            "on_breach": spec_params.on_breach,
            "halt_reentry_mode": spec_params.halt_reentry_mode,
            "halt_cooldown_days": spec_params.halt_cooldown_days,
        },
        "sizing": {
            "scheme": spec_params.sizing_scheme,
            "max_weight": spec_params.max_weight,
            "top_k_per_narrative": spec_params.top_k_per_narrative,
            "no_trade_band": spec_params.no_trade_band,
        },
        "costs": {
            "fee_bps": spec_params.fee_bps,
            "slippage_bps": spec_params.slippage_bps,
        },
        "narrative_map_ref": map_ref,
        "selected_narratives": selected,
    }

    if spec_params.method == "cross_sectional_momentum":
        # Same engine, but momentum ranks the whole priceable universe and the
        # CMC narrative signal feeds in as a score tilt (not a hard rotation).
        spec["method"] = "cross_sectional_momentum"
        spec["description"] = (
            "Systematic crypto-desk strategy: long/flat cross-sectional "
            "risk-adjusted momentum on the eligible BEP-20 universe, tilted toward "
            "tokens in the leading CoinMarketCap narratives, inverse-vol sized with "
            "portfolio vol targeting, gated by a BTC 200-DMA trend regime and a "
            "max-drawdown halt."
        )
        spec["momentum"] = {
            "lookbacks_days": [int(x) for x in spec_params.momentum_lookbacks_days],
            "skip_days": spec_params.momentum_skip_days,
            "top_n": spec_params.momentum_top_n,
            "risk_adjusted": spec_params.momentum_risk_adjusted,
            "narrative_tilt": spec_params.narrative_tilt,
        }
        # Momentum selects across the whole universe and sizes inverse-vol to a
        # portfolio vol target (schema-valid overrides of the rotation defaults).
        spec["selection"] = {
            "from": "eligible_universe",
            "filter": "eligible_bep20",
            "weight": "inverse_vol",
        }
        spec["sizing"] = {
            "scheme": "inverse_vol",
            "max_weight": spec_params.max_weight,
            "vol_target": spec_params.vol_target,
            "vol_lookback_days": spec_params.vol_lookback_days,
            "no_trade_band": spec_params.no_trade_band,
        }

    return spec


def _round_opt(value: Any, ndigits: int = 6) -> Optional[float]:
    if value is None:
        return None
    try:
        return round(float(value), ndigits)
    except (TypeError, ValueError):
        return None


def load_schema(path: Path = SCHEMA_PATH) -> Dict[str, Any]:
    return json.loads(Path(path).read_text())


def validate_spec(spec: Dict[str, Any], schema_path: Path = SCHEMA_PATH) -> None:
    """Validate ``spec`` against the strategy schema; raise on the first error."""
    validator = Draft202012Validator(load_schema(schema_path))
    errors = sorted(validator.iter_errors(spec), key=lambda e: list(e.path))
    if errors:
        first = errors[0]
        location = "/".join(str(p) for p in first.path) or "<root>"
        raise ValueError(
            f"strategy_spec failed schema validation at {location}: {first.message} "
            f"({len(errors)} error(s) total)"
        )


def write_spec(spec: Dict[str, Any], path: Path = DEFAULT_SPEC_PATH) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(spec, indent=2) + "\n")
    return path


def _load_or_build_map(
    client: CMCClient,
    eligible_tokens: List[Dict[str, Any]],
    rebuild: bool,
) -> Dict[str, Any]:
    """Use the frozen narrative->token map if present, else build it live."""
    if not rebuild and NARRATIVE_MAP_PATH.exists():
        return load_narrative_token_map()
    print("Building narrative -> token map live (frozen snapshot missing or --rebuild-map) ...")
    doc = build_narrative_token_map(client, eligible_tokens=eligible_tokens)
    write_narrative_token_map(doc)
    return doc


def generate_spec(
    client: Optional[CMCClient] = None,
    spec_params: Optional[SpecParams] = None,
    signal_params: Optional[SignalParams] = None,
    rebuild_map: bool = False,
) -> Dict[str, Any]:
    """End-to-end: rank live narratives, load selection map, build + validate spec."""
    client = client or CMCClient()
    signal_params = signal_params or SignalParams()
    spec_params = spec_params or SpecParams()

    signal_df = rank_narratives(client=client, params=signal_params)
    if signal_df.empty:
        raise RuntimeError("No narratives returned from trending_crypto_narratives.")

    eligible = load_eligible_tokens()
    narrative_map = _load_or_build_map(client, eligible, rebuild_map)

    spec = build_strategy_spec(
        signal_df, narrative_map, signal_params=signal_params, spec_params=spec_params
    )
    validate_spec(spec)
    return spec


def _print_spec_summary(spec: Dict[str, Any], out_path: Path) -> None:
    selected = spec.get("selected_narratives", [])
    method = spec.get("method", "narrative_rotation")
    if method == "cross_sectional_momentum":
        mom = spec.get("momentum", {})
        print(
            f"\nNarrativeAlpha strategy spec - {spec['name']}  "
            f"[method: cross_sectional_momentum]\n"
            f"  Momentum: lookbacks={mom.get('lookbacks_days')} skip={mom.get('skip_days')}d "
            f"top_n={mom.get('top_n')} risk_adjusted={mom.get('risk_adjusted')}\n"
            f"  CMC narrative tilt: {mom.get('narrative_tilt')}  "
            f"(boosts momentum scores of tokens in the leading narratives below)\n"
        )
    else:
        print(
            f"\nNarrativeAlpha strategy spec - {spec['name']} "
            f"(top {spec['signal']['top_n_narratives']} narratives)\n"
        )
    for nar in selected:
        flag = "  [exhausted]" if nar.get("exhausted") else ""
        syms = [t["symbol"] for t in nar.get("tokens", [])]
        print(f"#{nar['rank']}  {nar['narrative']}{flag}")
        print(
            f"     score={nar['score']:+.4f}  "
            f"perf_7d={_fmt_pct(nar.get('perf_7d'))}  "
            f"perf_30d={_fmt_pct(nar.get('perf_30d'))}  "
            f"{len(syms)} eligible tokens"
        )
        print(f"     tokens: {', '.join(syms[:10])}{' ...' if len(syms) > 10 else ''}\n")
    print(f"Schema-valid spec written -> {out_path}")


def main(argv: Optional[List[str]] = None) -> int:
    """Phase 3 acceptance: build + validate + write a strategy spec from live CMC data."""
    parser = argparse.ArgumentParser(
        description="Build a schema-valid NarrativeAlpha strategy_spec.json from live CMC data."
    )
    parser.add_argument(
        "-o", "--out", default=str(DEFAULT_SPEC_PATH),
        help=f"Output path for the spec (default: {DEFAULT_SPEC_PATH}).",
    )
    parser.add_argument(
        "--example", action="store_true",
        help=f"Also write the committed example to {EXAMPLE_SPEC_PATH}.",
    )
    parser.add_argument(
        "--top-n", type=int, default=None,
        help="Override the number of narratives to hold/feed the tilt (default: 3).",
    )
    parser.add_argument(
        "--method", default="narrative_rotation",
        choices=["narrative_rotation", "narrative", "cross_sectional_momentum", "momentum"],
        help="Strategy engine: 'momentum' = narrative-fed cross-sectional momentum "
             "(recommended); 'narrative' = pure CMC narrative rotation. Default: narrative.",
    )
    parser.add_argument(
        "--rebuild-map", action="store_true",
        help="Rebuild the narrative->token map live instead of using the frozen snapshot.",
    )
    args = parser.parse_args(argv)

    method = {
        "momentum": "cross_sectional_momentum",
        "narrative": "narrative_rotation",
    }.get(args.method, args.method)
    is_momentum = method == "cross_sectional_momentum"
    spec_params = SpecParams(
        method=method,
        name="CryptoMomentum v1" if is_momentum else "NarrativeAlpha v1",
        top_n_narratives=args.top_n if args.top_n is not None else SpecParams.top_n_narratives,
    )

    spec = generate_spec(spec_params=spec_params, rebuild_map=args.rebuild_map)

    out_path = write_spec(spec, Path(args.out))
    _print_spec_summary(spec, out_path)

    if args.example:
        example_path = write_spec(spec, EXAMPLE_SPEC_PATH)
        print(f"Example spec written -> {example_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
