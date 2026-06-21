"""NarrativeAlpha deterministic backtest engine (Phase 4).

Backtests a ``strategy_spec.json`` on real price history with no look-ahead.

Design (see PRD sec.7-8): the CMC narrative tool is live-only, so we freeze a
narrative->token map (Phase 2) and reconstruct each narrative's sector basket
from free Binance daily klines (Phase 4 ``data.py``). The live ranking score is
not available historically, so we rank baskets by a **price-based proxy** of the
same idea - trailing relative strength vs the total market - using only data
available at each rebalance.

Pipeline per run:
  1. Load the spec + its *frozen* narrative map + the eligible universe.
  2. Load aligned open/close panels for every eligible pair (cached on disk).
  3. Reconstruct an equal-weight daily-rebalanced index per narrative basket and
     an equal-weight index for the whole universe (the market proxy).
  4. Weekly rotation: at each rebalance, score baskets (relative 7d/30d strength
     + absolute confirmation), down-rank exhausted blow-offs, hold the top-N,
     equal-weight their priceable eligible constituents, and fill at next open.
  5. Risk gates: go to cash when Fear & Greed <= floor or the market trend is
     down; flatten on a max-drawdown breach and stay flat until risk-off clears.
  6. Charge fee + slippage on turnover at each fill.

Look-ahead safety: the signal at day ``d`` uses only closes through ``d`` and is
filled at the **open of ``d+1``**; the entry day's P&L is open->close, every
later day is close->close.

CLI::

    python scripts/backtest.py examples/strategy_spec.example.json
    python scripts/backtest.py spec.json --start 2022-01-01 --out .cache/backtest
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

try:  # Allow both ``python scripts/backtest.py`` and ``import``.
    import data as price_data
    from universe import load_eligible_tokens, load_narrative_token_map
except ImportError:  # pragma: no cover - package-style import
    from scripts import data as price_data
    from scripts.universe import load_eligible_tokens, load_narrative_token_map


_REPO_ROOT = Path(__file__).resolve().parent.parent

# Trailing windows (in trading days) used by the historical scoring proxy. These
# mirror the live spec's 7d/30d ranking horizons.
_WIN_24H = 1
_WIN_7D = 7
_WIN_30D = 30
_WARMUP = _WIN_30D + 1          # rows needed before the first valid score
_TRADING_DAYS = 365            # crypto trades daily -> annualization factor
_BENCH_PAIR = "BTCUSDT"

# Strategy methods the engine can run.
METHOD_NARRATIVE = "narrative_rotation"
METHOD_MOMENTUM = "cross_sectional_momentum"


# ============================================================================ #
# Spec parameters                                                              #
# ============================================================================ #
@dataclass(frozen=True)
class BacktestParams:
    """Flattened, typed view of the knobs the engine reads from a spec."""

    freq_days: int = 14
    top_n: int = 3
    w_perf_7d: float = 0.5
    w_perf_30d: float = 0.3
    w_confirm_7d: float = 0.2
    exh_24h_below: float = -0.02
    exh_30d_above: float = 0.10
    exh_penalty: float = 0.5
    fear_greed_min: float = 25.0
    max_drawdown: float = 0.25
    max_weight: float = 0.15
    fee_bps: float = 10.0
    slippage_bps: float = 5.0

    # --- timing: slow trend filter (replaces the old coincident 30d-trend gate) ---
    # Long only while BTC holds above its long moving average; Fear & Greed is a
    # true-extreme override, not a coincident on/off switch. A symmetric band
    # around the MA adds hysteresis so the regime does not flip week to week.
    trend_ma_days: int = 200
    trend_band: float = 0.03          # +/- 3% around the MA before flipping regime
    fear_greed_extreme: float = 12.0  # cash only on genuine panic

    # After a max-drawdown flatten, how we allow re-entry:
    #   "next_clear"   - re-enter at the next non-risk-off rebalance
    #   "cooldown"     - re-enter once regime is on AND cooldown_days have passed
    #   "regime_reset" - re-enter only after the trend regime cycles off->on
    # regime_reset tests best over a full cycle: it stops us re-buying into the
    # same downtrend after a drawdown halt.
    halt_reentry_mode: str = "regime_reset"
    halt_cooldown_days: int = 30

    # --- turnover control ---
    # Hold only each narrative's strongest few names (cuts the ~100%/rebalance
    # churn from equal-weighting ~75 overlapping tokens), and skip trades smaller
    # than a no-trade band so positions that barely drift are left alone.
    top_k_per_narrative: int = 8
    no_trade_band: float = 0.03

    # --- method + cross-sectional momentum (systematic crypto-desk style) ---
    method: str = METHOD_NARRATIVE
    sizing_scheme: str = "equal_weight"     # "equal_weight" | "inverse_vol"
    mom_lookbacks: Tuple[int, ...] = (30, 60, 90)  # trailing-return windows (days)
    mom_skip_days: int = 7                  # skip most-recent days (reversal guard)
    mom_top_n: int = 15                     # number of names held (~top decile)
    mom_risk_adjusted: bool = True          # divide momentum by trailing vol
    vol_lookback_days: int = 30             # window for the vol estimate
    vol_target: float = 0.40                # annualized portfolio vol target (0 = off)
    narrative_tilt: float = 0.0             # optional score boost for in-narrative names

    @property
    def cost_rate(self) -> float:
        return (self.fee_bps + self.slippage_bps) / 1e4

    @classmethod
    def from_spec(cls, spec: Dict[str, Any]) -> "BacktestParams":
        sig = spec.get("signal", {})
        weights = sig.get("weights", {}) or {}
        exh = sig.get("exhaustion_filter", {}) or {}
        risk_off = spec.get("risk_off", {}) or {}
        sizing = spec.get("sizing", {}) or {}
        mom = spec.get("momentum", {}) or {}
        return cls(
            freq_days=int(spec.get("rebalance", {}).get("frequency_days", 14)),
            top_n=int(sig.get("top_n_narratives", 3)),
            w_perf_7d=float(weights.get("perf_7d", 0.5)),
            w_perf_30d=float(weights.get("perf_30d", 0.3)),
            w_confirm_7d=float(weights.get("confirm_mc_7d", 0.2)),
            exh_24h_below=float(exh.get("perf_24h_below", -0.02)),
            exh_30d_above=float(exh.get("perf_30d_above", 0.10)),
            exh_penalty=float(exh.get("penalty", 0.5)),
            fear_greed_min=float(risk_off.get("fear_greed_min", 25.0)),
            max_drawdown=float(spec.get("risk", {}).get("max_drawdown", 0.25)),
            max_weight=float(sizing.get("max_weight", 0.15)),
            fee_bps=float(spec.get("costs", {}).get("fee_bps", 10.0)),
            slippage_bps=float(spec.get("costs", {}).get("slippage_bps", 5.0)),
            # Optional overrides if a spec chooses to carry them (schema-compatible
            # defaults are used otherwise).
            trend_ma_days=int(risk_off.get("trend_ma_days", 200)),
            trend_band=float(risk_off.get("trend_band", 0.03)),
            fear_greed_extreme=float(risk_off.get("fear_greed_extreme", 12.0)),
            top_k_per_narrative=int(sizing.get("top_k_per_narrative", 8)),
            no_trade_band=float(sizing.get("no_trade_band", 0.03)),
            halt_reentry_mode=str(spec.get("risk", {}).get("halt_reentry_mode", "regime_reset")),
            halt_cooldown_days=int(spec.get("risk", {}).get("halt_cooldown_days", 30)),
            method=str(spec.get("method", METHOD_NARRATIVE)),
            sizing_scheme=str(sizing.get("scheme", "equal_weight")),
            mom_lookbacks=tuple(int(x) for x in mom.get("lookbacks_days", (30, 60, 90))),
            mom_skip_days=int(mom.get("skip_days", 7)),
            mom_top_n=int(mom.get("top_n", 15)),
            mom_risk_adjusted=bool(mom.get("risk_adjusted", True)),
            vol_lookback_days=int(sizing.get("vol_lookback_days", 30)),
            vol_target=float(sizing.get("vol_target", 0.40)),
            narrative_tilt=float(mom.get("narrative_tilt", 0.0)),
        )


@dataclass
class BacktestResult:
    """Everything the engine produces; consumed by report.py (Phase 5)."""

    equity: pd.Series
    returns: pd.Series
    weights: pd.DataFrame
    trades: pd.DataFrame
    rotation: pd.DataFrame
    benchmarks: pd.DataFrame
    metrics: Dict[str, float]
    params: BacktestParams
    spec: Dict[str, Any] = field(default_factory=dict)


# ============================================================================ #
# Basket / market reconstruction                                               #
# ============================================================================ #
def _equal_weight_index(returns: pd.DataFrame, columns: List[str]) -> pd.Series:
    """Equal-weight, daily-rebalanced index from a set of return columns.

    Missing constituents on a given day are simply excluded from that day's
    average (no look-ahead, no forward-fill of prices).
    """
    cols = [c for c in columns if c in returns.columns]
    if not cols:
        return pd.Series(1.0, index=returns.index, name="index")
    basket_ret = returns[cols].mean(axis=1, skipna=True).fillna(0.0)
    return (1.0 + basket_ret).cumprod()


def _trailing(idx: pd.Series, window: int) -> pd.Series:
    """Trailing simple return over ``window`` rows (NaN until enough history)."""
    return idx.pct_change(window, fill_method=None)


@dataclass
class _Basket:
    name: str
    slug: Optional[str]
    pairs: List[str]
    index: pd.Series
    rel_24h: pd.Series
    rel_7d: pd.Series
    rel_30d: pd.Series
    abs_7d: pd.Series


def _build_baskets(
    narratives: List[Dict[str, Any]],
    returns: pd.DataFrame,
    market_index: pd.Series,
) -> List[_Basket]:
    mkt_24h = _trailing(market_index, _WIN_24H)
    mkt_7d = _trailing(market_index, _WIN_7D)
    mkt_30d = _trailing(market_index, _WIN_30D)

    baskets: List[_Basket] = []
    for nar in narratives:
        pairs = [t["binance_pair"].upper() for t in nar.get("tokens", [])]
        priceable = [p for p in pairs if p in returns.columns]
        if not priceable:
            continue
        idx = _equal_weight_index(returns, priceable)
        baskets.append(
            _Basket(
                name=nar.get("narrative", "?"),
                slug=nar.get("slug"),
                pairs=priceable,
                index=idx,
                rel_24h=_trailing(idx, _WIN_24H) - mkt_24h,
                rel_7d=_trailing(idx, _WIN_7D) - mkt_7d,
                rel_30d=_trailing(idx, _WIN_30D) - mkt_30d,
                abs_7d=_trailing(idx, _WIN_7D),
            )
        )
    return baskets


# ============================================================================ #
# Scoring + selection                                                          #
# ============================================================================ #
def _score_basket(b: _Basket, i: int, p: BacktestParams) -> Optional[Tuple[float, bool]]:
    """Score one basket at positional date ``i``; ``None`` if not yet rankable."""
    rel_7d = b.rel_7d.iat[i]
    rel_30d = b.rel_30d.iat[i]
    abs_7d = b.abs_7d.iat[i]
    rel_24h = b.rel_24h.iat[i]
    if not np.isfinite(rel_7d) or not np.isfinite(rel_30d) or not np.isfinite(abs_7d):
        return None
    raw = p.w_perf_7d * rel_7d + p.w_perf_30d * rel_30d + p.w_confirm_7d * abs_7d
    exhausted = (
        np.isfinite(rel_24h)
        and rel_24h < p.exh_24h_below
        and rel_30d > p.exh_30d_above
    )
    score = raw * (1.0 - p.exh_penalty) if exhausted else raw
    return float(score), bool(exhausted)


def _apply_no_trade_band(
    target: Dict[str, float], active: Dict[str, float], band: float
) -> Dict[str, float]:
    """Suppress trades smaller than ``band`` to curb churn.

    For each pair, if the target weight is within ``band`` of the currently held
    weight, keep the current weight (don't trade); otherwise move to target. A
    target of 0 that differs from a held weight by >= band is a genuine exit and
    is honored. Full-flatten decisions (empty target) bypass this and are applied
    by the caller so risk-off / drawdown exits always go fully to cash.
    """
    final: Dict[str, float] = {}
    for pair in set(target) | set(active):
        tgt = target.get(pair, 0.0)
        cur = active.get(pair, 0.0)
        if abs(tgt - cur) < band:
            if cur > 0.0:
                final[pair] = cur          # leave a barely-drifting position alone
        elif tgt > 0.0:
            final[pair] = tgt              # meaningful move -> trade to target
        # tgt == 0 with a large held weight falls through -> sold (weight 0)
    return final


def _select_targets(
    baskets: List[_Basket],
    i: int,
    date: pd.Timestamp,
    closes: pd.DataFrame,
    tok_mom: pd.DataFrame,
    active: Dict[str, float],
    p: BacktestParams,
) -> Tuple[Dict[str, float], List[Dict[str, Any]]]:
    """Rank baskets at ``i``, hold top-N, equal-weight each basket's strongest
    ``top_k_per_narrative`` priceable names, then apply the no-trade band.

    Returns ``(target_weights_by_pair, held_narratives_meta)``.
    """
    scored = []
    for b in baskets:
        result = _score_basket(b, i, p)
        if result is None:
            continue
        score, exhausted = result
        scored.append((score, exhausted, b))
    # Deterministic ordering: score desc, then name for tie-breaks.
    scored.sort(key=lambda s: (-s[0], s[2].name))
    held = scored[: p.top_n]
    if not held:
        return {}, []

    num_held = len(held)
    weights: Dict[str, float] = {}
    meta: List[Dict[str, Any]] = []
    for score, exhausted, b in held:
        priceable = [pr for pr in b.pairs if np.isfinite(closes.at[date, pr])]
        if not priceable:
            continue
        # Keep only the strongest names in the basket (trailing 7d momentum),
        # which both sharpens selection and shrinks turnover.
        priceable.sort(
            key=lambda pr: (
                tok_mom.at[date, pr] if np.isfinite(tok_mom.at[date, pr]) else -np.inf
            ),
            reverse=True,
        )
        chosen = priceable[: p.top_k_per_narrative]
        per_token = min((1.0 / num_held) / len(chosen), p.max_weight)
        for pr in chosen:
            weights[pr] = weights.get(pr, 0.0) + per_token
        meta.append(
            {
                "narrative": b.name,
                "slug": b.slug,
                "score": round(score, 6),
                "exhausted": exhausted,
                "n_tokens": len(chosen),
            }
        )

    weights = _apply_no_trade_band(weights, active, p.no_trade_band)
    return weights, meta


# ============================================================================ #
# Cross-sectional momentum (systematic crypto-desk method)                     #
# ============================================================================ #
def _momentum_score_panel(
    closes: pd.DataFrame, lookbacks: Tuple[int, ...], skip: int
) -> pd.DataFrame:
    """Blended trailing-return momentum that skips the most recent ``skip`` days.

    For each lookback ``L`` the score uses the return from ``t-skip-L`` to
    ``t-skip`` (so the latest, mean-reverting days are excluded), then averages
    across lookbacks. Pure function of past closes -> look-ahead free.
    """
    ended = closes.shift(skip)
    parts = [ended / closes.shift(skip + L) - 1.0 for L in lookbacks]
    return sum(parts) / float(len(parts))


def _leading_narrative_boost(
    baskets: List["_Basket"], i: int, p: BacktestParams
) -> Dict[str, float]:
    """Membership weight in the currently leading CMC narratives (momentum tilt).

    Scores every narrative basket at positional date ``i`` with the same
    relative-strength scorer used by narrative rotation, keeps the top ``p.top_n``
    baskets with positive score, and returns ``{pair: weight}`` where ``weight``
    decays linearly by narrative rank (the hottest narrative -> 1.0). A token that
    sits in several leading narratives keeps its strongest membership. Basket
    scores use only trailing windows, so this is look-ahead free.
    """
    if not baskets or p.narrative_tilt <= 0.0:
        return {}
    scored: List[Tuple[float, "_Basket"]] = []
    for b in baskets:
        res = _score_basket(b, i, p)
        if res is None:
            continue
        score, _exhausted = res
        if score > 0.0:
            scored.append((score, b))
    if not scored:
        return {}
    scored.sort(key=lambda s: (-s[0], s[1].name))
    leaders = scored[: max(p.top_n, 1)]
    n = len(leaders)
    boost: Dict[str, float] = {}
    for rank, (_score, b) in enumerate(leaders):
        w = 1.0 - rank / n  # hottest narrative -> 1.0, decaying by rank
        for pr in b.pairs:
            if w > boost.get(pr, 0.0):
                boost[pr] = w
    return boost


def _select_targets_momentum(
    i: int,
    date: pd.Timestamp,
    closes: pd.DataFrame,
    mom_score: pd.DataFrame,
    vol_ann: pd.DataFrame,
    eligible_pairs: List[str],
    active: Dict[str, float],
    p: BacktestParams,
    baskets: Optional[List["_Basket"]] = None,
) -> Tuple[Dict[str, float], List[Dict[str, Any]]]:
    """Rank the universe by (risk-adjusted) momentum, hold top-N, size by
    inverse-vol, then scale gross to the portfolio vol target.

    When ``narrative_tilt > 0`` the risk-adjusted momentum score of every token
    that belongs to a currently leading CMC narrative is boosted by
    ``narrative_tilt * membership_weight``, so the CMC narrative signal feeds
    directly into which names momentum selects (a tilt, not a hard filter).
    """
    boost = _leading_narrative_boost(baskets or [], i, p)
    scores: List[Tuple[float, str]] = []
    for pair in eligible_pairs:
        s = mom_score.at[date, pair]
        v = vol_ann.at[date, pair]
        if not np.isfinite(s) or not np.isfinite(closes.at[date, pair]):
            continue
        if p.mom_risk_adjusted:
            if not np.isfinite(v) or v <= 0:
                continue
            s = s / v
        if boost:
            s += p.narrative_tilt * boost.get(pair, 0.0)
        scores.append((float(s), pair))
    if not scores:
        return {}, []

    # Highest momentum first; only positive-momentum names (don't buy losers).
    scores.sort(key=lambda x: (-x[0], x[1]))
    chosen = [(s, pr) for s, pr in scores if s > 0][: p.mom_top_n]
    if not chosen:
        return {}, []

    # Inverse-vol weights (risk parity-lite); fall back to equal weight.
    raw: Dict[str, float] = {}
    for _s, pr in chosen:
        v = vol_ann.at[date, pr]
        if p.sizing_scheme == "inverse_vol" and np.isfinite(v) and v > 0:
            raw[pr] = 1.0 / v
        else:
            raw[pr] = 1.0
    tot = sum(raw.values())
    weights = {pr: wv / tot for pr, wv in raw.items()}

    # Volatility targeting: scale gross exposure down when the book is too hot.
    # Portfolio vol proxy assumes high correlation (conservative) = sum w*vol.
    if p.vol_target and p.vol_target > 0:
        port_vol = sum(weights[pr] * vol_ann.at[date, pr] for pr in weights
                       if np.isfinite(vol_ann.at[date, pr]))
        if port_vol > 0:
            gross = min(1.0, p.vol_target / port_vol)
            weights = {pr: w * gross for pr, w in weights.items()}

    # Per-name cap.
    weights = {pr: min(w, p.max_weight) for pr, w in weights.items()}

    meta = [
        {"narrative": _pair_to_symbol(pr), "slug": None,
         "score": round(s, 4), "exhausted": False, "n_tokens": 1,
         "narrative_tilted": pr in boost}
        for s, pr in chosen
    ]
    weights = _apply_no_trade_band(weights, active, p.no_trade_band)
    return weights, meta


def _pair_to_symbol(pair: str) -> str:
    return pair[:-4] if pair.endswith("USDT") else pair


# ============================================================================ #
# Engine                                                                       #
# ============================================================================ #
class Backtester:
    def __init__(
        self,
        spec: Dict[str, Any],
        opens: pd.DataFrame,
        closes: pd.DataFrame,
        narratives: List[Dict[str, Any]],
        fear_greed: pd.Series,
        eligible_pairs: List[str],
    ) -> None:
        self.spec = spec
        self.params = BacktestParams.from_spec(spec)
        self.opens = opens
        self.closes = closes
        self.dates = closes.index
        self.narratives = narratives
        self.eligible_pairs = [p for p in eligible_pairs if p in closes.columns]

        # Daily simple returns (close-to-close) and same-day open-to-close.
        self.ret_cc = closes.pct_change(fill_method=None)
        self.ret_oc = (closes - opens) / opens

        # Per-token trailing 7d momentum, used to keep each basket's top names.
        self.tok_ret_7d = closes.pct_change(_WIN_7D, fill_method=None)

        # Cross-sectional momentum inputs (used by METHOD_MOMENTUM).
        self.tok_vol_ann = (
            self.ret_cc.rolling(self.params.vol_lookback_days).std() * np.sqrt(_TRADING_DAYS)
        )
        self.mom_score = _momentum_score_panel(
            closes, self.params.mom_lookbacks, self.params.mom_skip_days
        )

        # Market proxy = equal-weight of the full eligible universe.
        self.market_index = _equal_weight_index(self.ret_cc, self.eligible_pairs)
        self.market_ret_30d = _trailing(self.market_index, _WIN_30D)

        self.baskets = _build_baskets(narratives, self.ret_cc, self.market_index)

        # Daily Fear & Greed aligned to the trading calendar (carry last reading).
        self.fng = fear_greed.reindex(self.dates).ffill()

        # Slow trend regime with hysteresis (precomputed, deterministic).
        self.regime_on = self._compute_regime()

    def _compute_regime(self) -> pd.Series:
        """Daily long/flat regime from BTC vs its long MA, with a hysteresis band.

        Risk-on turns on when BTC closes above ``MA * (1 + band)`` and off when it
        closes below ``MA * (1 - band)``; between the bands the prior state is
        held, so the regime doesn't flip on small wiggles around the MA. Falls
        back to the equal-weight universe index if BTC is unavailable. Undefined
        (pre-MA-warmup) days are treated as flat.
        """
        p = self.params
        ref = self.closes[_BENCH_PAIR] if _BENCH_PAIR in self.closes.columns else self.market_index
        ma = ref.rolling(p.trend_ma_days, min_periods=p.trend_ma_days).mean()
        upper = ma * (1.0 + p.trend_band)
        lower = ma * (1.0 - p.trend_band)

        regime = pd.Series(False, index=self.dates)
        state = False
        ref_vals, up_vals, lo_vals = ref.to_numpy(), upper.to_numpy(), lower.to_numpy()
        for k in range(len(self.dates)):
            price, hi, lo = ref_vals[k], up_vals[k], lo_vals[k]
            if np.isfinite(hi) and np.isfinite(price):
                if price > hi:
                    state = True
                elif price < lo:
                    state = False
            else:
                state = False  # MA not yet defined -> stay flat
            regime.iat[k] = state
        return regime

    # ------------------------------------------------------------------ run --
    def _rebalance_indices(self) -> List[int]:
        return list(range(_WARMUP, len(self.dates), self.params.freq_days))

    def _is_risk_off(self, i: int) -> bool:
        # Out of the market unless the slow trend regime is on...
        if not bool(self.regime_on.iat[i]):
            return True
        # ...or Fear & Greed signals genuine panic (extreme override only).
        fng = self.fng.iat[i]
        if np.isfinite(fng) and fng <= self.params.fear_greed_extreme:
            return True
        return False

    def _halt_cleared(self, i: int, halt_index: int, regime_reset: bool) -> bool:
        """Whether re-entry is allowed after a max-drawdown halt (see params)."""
        mode = self.params.halt_reentry_mode
        if mode == "next_clear":
            return True
        if mode == "cooldown":
            return (i - halt_index) >= self.params.halt_cooldown_days
        return regime_reset  # "regime_reset"

    def run(self) -> BacktestResult:
        p = self.params
        dates = self.dates
        n = len(dates)
        rebal = set(self._rebalance_indices())

        equity = 1.0
        peak = 1.0
        active: Dict[str, float] = {}      # weights contributing to today's P&L
        pending: Optional[Dict[str, float]] = None  # to be filled at today's open
        halted = False
        halt_index = -1                    # row where the last DD halt fired
        regime_reset_since_halt = False    # has the trend regime cycled off->on?

        equity_series: List[float] = []
        ret_series: List[float] = []
        weight_rows: List[Dict[str, float]] = []
        trades: List[Dict[str, Any]] = []
        rotation: List[Dict[str, Any]] = []

        for i in range(n):
            date = dates[i]
            filled_today = False

            # 1) Settle any pending order at today's open (next-open fill).
            if pending is not None:
                turnover = self._turnover(active, pending)
                cost = turnover * p.cost_rate
                equity *= (1.0 - cost)
                self._log_trades(trades, date, active, pending, turnover, cost)
                active = pending
                pending = None
                filled_today = bool(active)

            # 2) Today's portfolio P&L (entry day uses open->close).
            r = self._portfolio_return(i, date, active, filled_today)
            equity *= (1.0 + r)

            # 3) Drawdown gate (checked daily; flatten at next open). After a
            # breach we stay flat until the trend regime fully resets (turns off
            # then back on), so we don't re-enter the same losing leg.
            peak = max(peak, equity)
            drawdown = equity / peak - 1.0
            if not halted and active and drawdown <= -p.max_drawdown:
                halted = True
                halt_index = i
                regime_reset_since_halt = False
                pending = {}  # flatten next open
            if halted and not bool(self.regime_on.iat[i]):
                regime_reset_since_halt = True  # regime went risk-off; arm re-entry

            # 4) Scheduled rebalance (decision uses data through today's close).
            if i in rebal and pending is None:
                risk_off = self._is_risk_off(i)
                if halted:
                    if not risk_off and self._halt_cleared(i, halt_index, regime_reset_since_halt):
                        halted = False
                        target, meta = self._decide(i, date, risk_off, active)
                    else:
                        target, meta = {}, []  # stay flat until re-entry is allowed
                else:
                    target, meta = self._decide(i, date, risk_off, active)
                pending = target
                rotation.append(
                    {
                        "date": date,
                        "risk_off": risk_off,
                        "halted": halted,
                        "n_held": len(meta),
                        "narratives": ", ".join(m["narrative"] for m in meta) or "CASH",
                        "detail": meta,
                    }
                )

            equity_series.append(equity)
            ret_series.append(r)
            weight_rows.append(dict(active))

        return self._assemble(dates, equity_series, ret_series, weight_rows, trades, rotation)

    # -------------------------------------------------------------- helpers --
    def _decide(
        self, i: int, date: pd.Timestamp, risk_off: bool, active: Dict[str, float]
    ) -> Tuple[Dict[str, float], List[Dict[str, Any]]]:
        if risk_off:
            return {}, []
        if self.params.method == METHOD_MOMENTUM:
            return _select_targets_momentum(
                i, date, self.closes, self.mom_score, self.tok_vol_ann,
                self.eligible_pairs, active, self.params, self.baskets,
            )
        return _select_targets(
            self.baskets, i, date, self.closes, self.tok_ret_7d, active, self.params
        )

    @staticmethod
    def _turnover(old: Dict[str, float], new: Dict[str, float]) -> float:
        keys = set(old) | set(new)
        return sum(abs(new.get(k, 0.0) - old.get(k, 0.0)) for k in keys)

    def _portfolio_return(
        self, i: int, date: pd.Timestamp, active: Dict[str, float], filled_today: bool
    ) -> float:
        if not active or i == 0:
            return 0.0
        source = self.ret_oc if filled_today else self.ret_cc
        total = 0.0
        for pair, w in active.items():
            if pair not in source.columns:
                continue
            r = source.at[date, pair]
            if np.isfinite(r):
                total += w * r
        return total

    @staticmethod
    def _log_trades(
        trades: List[Dict[str, Any]],
        date: pd.Timestamp,
        old: Dict[str, float],
        new: Dict[str, float],
        turnover: float,
        cost: float,
    ) -> None:
        keys = sorted(set(old) | set(new))
        for pair in keys:
            w_before = old.get(pair, 0.0)
            w_after = new.get(pair, 0.0)
            if abs(w_after - w_before) < 1e-9:
                continue
            trades.append(
                {
                    "date": date,
                    "pair": pair,
                    "side": "buy" if w_after > w_before else "sell",
                    "weight_before": round(w_before, 6),
                    "weight_after": round(w_after, 6),
                    "weight_delta": round(w_after - w_before, 6),
                    "turnover": round(turnover, 6),
                    "cost": round(cost, 8),
                }
            )

    # ------------------------------------------------------------ assemble --
    def _assemble(
        self,
        dates: pd.DatetimeIndex,
        equity_series: List[float],
        ret_series: List[float],
        weight_rows: List[Dict[str, float]],
        trades: List[Dict[str, Any]],
        rotation: List[Dict[str, Any]],
    ) -> BacktestResult:
        equity = pd.Series(equity_series, index=dates, name="equity").astype(float)
        returns = pd.Series(ret_series, index=dates, name="return").astype(float)
        weights = pd.DataFrame(weight_rows, index=dates).fillna(0.0)
        trades_df = (
            pd.DataFrame(trades)
            if trades
            else pd.DataFrame(
                columns=["date", "pair", "side", "weight_before", "weight_after",
                         "weight_delta", "turnover", "cost"]
            )
        )
        rotation_df = (
            pd.DataFrame(rotation)
            if rotation
            else pd.DataFrame(columns=["date", "risk_off", "halted", "n_held", "narratives"])
        )
        benchmarks = self._benchmarks(dates)
        metrics = compute_metrics(equity, returns, trades_df, weights)
        return BacktestResult(
            equity=equity,
            returns=returns,
            weights=weights,
            trades=trades_df,
            rotation=rotation_df,
            benchmarks=benchmarks,
            metrics=metrics,
            params=self.params,
            spec=self.spec,
        )

    def _benchmarks(self, dates: pd.DatetimeIndex) -> pd.DataFrame:
        cols: Dict[str, pd.Series] = {}
        if _BENCH_PAIR in self.closes.columns:
            btc = self.closes[_BENCH_PAIR]
            base = btc.dropna()
            if not base.empty:
                cols["btc_hodl"] = (btc / base.iloc[0]).reindex(dates).ffill()
        cols["ew_universe"] = self.market_index / self.market_index.iloc[0]

        # No-rotation baseline: hold *all* narrative baskets equal-weighted for
        # the whole window (each basket is itself daily equal-weighted across its
        # constituents). This isolates the value added by rotation -- our
        # strategy should beat simply owning every narrative all the time.
        if self.baskets:
            basket_rets = pd.DataFrame(
                {b.name: b.index.pct_change(fill_method=None) for b in self.baskets}
            )
            no_rot_ret = basket_rets.mean(axis=1, skipna=True).fillna(0.0)
            no_rot = (1.0 + no_rot_ret).cumprod()
            cols["no_rotation"] = (no_rot / no_rot.iloc[0]).reindex(dates).ffill()

        return pd.DataFrame(cols).reindex(dates)


# ============================================================================ #
# Metrics                                                                       #
# ============================================================================ #
def compute_metrics(
    equity: pd.Series,
    returns: pd.Series,
    trades: pd.DataFrame,
    weights: pd.DataFrame,
) -> Dict[str, float]:
    eq = equity.dropna()
    if eq.empty:
        return {}
    total_return = float(eq.iloc[-1] / eq.iloc[0] - 1.0)
    n_days = len(eq)
    years = n_days / _TRADING_DAYS
    cagr = float(eq.iloc[-1] ** (1.0 / years) - 1.0) if years > 0 and eq.iloc[-1] > 0 else float("nan")

    r = returns.fillna(0.0)
    vol = float(r.std(ddof=0) * np.sqrt(_TRADING_DAYS))
    mean_ann = float(r.mean() * _TRADING_DAYS)
    sharpe = mean_ann / vol if vol > 0 else float("nan")
    downside = r[r < 0]
    dstd = float(downside.std(ddof=0) * np.sqrt(_TRADING_DAYS))
    sortino = mean_ann / dstd if dstd > 0 else float("nan")

    running_peak = eq.cummax()
    drawdown = eq / running_peak - 1.0
    max_dd = float(drawdown.min())
    calmar = (cagr / abs(max_dd)) if max_dd < 0 and np.isfinite(cagr) else float("nan")

    invested = (weights.sum(axis=1) > 1e-9)
    exposure = float(invested.mean())
    invested_days = r[invested]
    hit_rate = float((invested_days > 0).mean()) if len(invested_days) else float("nan")
    total_turnover = float(trades["turnover"].groupby(trades["date"]).first().sum()) if not trades.empty else 0.0
    total_cost = float(trades["cost"].groupby(trades["date"]).first().sum()) if not trades.empty else 0.0

    return {
        "total_return": total_return,
        "cagr": cagr,
        "ann_vol": vol,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown": max_dd,
        "calmar": calmar,
        "exposure": exposure,
        "hit_rate": hit_rate,
        "n_rebalances": int(trades["date"].nunique()) if not trades.empty else 0,
        "total_turnover": total_turnover,
        "total_cost": total_cost,
        "n_days": int(n_days),
    }


# ============================================================================ #
# Orchestration                                                                #
# ============================================================================ #
def load_spec(path: str | Path) -> Dict[str, Any]:
    return json.loads(Path(path).read_text())


def _resolve_ref(spec: Dict[str, Any], spec_path: Path) -> Optional[Path]:
    ref = spec.get("narrative_map_ref")
    if not ref:
        return None
    candidate = (_REPO_ROOT / ref)
    return candidate if candidate.exists() else (spec_path.parent / ref)


def run_backtest(
    spec_path: str | Path,
    start: str = price_data._DEFAULT_START,
    end: Optional[str] = None,
    use_cache: bool = True,
    verbose: bool = True,
    spec: Optional[Dict[str, Any]] = None,
) -> BacktestResult:
    """End-to-end backtest of a strategy spec on real Binance price history.

    ``spec_path`` is always used to resolve a relative ``narrative_map_ref``. Pass
    ``spec`` to backtest an in-memory variant of that file (e.g. report.py's
    cost-sensitivity sweep) without writing it to disk.
    """
    spec_path = Path(spec_path)
    if spec is None:
        spec = load_spec(spec_path)

    map_path = _resolve_ref(spec, spec_path)
    narrative_map = (
        load_narrative_token_map(map_path) if map_path else load_narrative_token_map()
    )
    narratives = narrative_map.get("narratives", [])
    if not narratives:
        raise RuntimeError("Frozen narrative map has no narratives to backtest.")

    eligible = load_eligible_tokens()
    # Only tokens with a Binance *USDT spot pair are priceable for the backtest;
    # DEX-only BEP-20 names (binance_pair=null / priceable=false) have no CEX
    # OHLCV history and are skipped.
    eligible_pairs = [
        t["binance_pair"].upper()
        for t in eligible
        if t.get("binance_pair")
    ]
    # Make sure every basket constituent and the BTC benchmark are loaded too.
    basket_pairs = {
        t["binance_pair"].upper()
        for nar in narratives
        for t in nar.get("tokens", [])
        if t.get("binance_pair")
    }
    pairs = sorted(set(eligible_pairs) | basket_pairs | {_BENCH_PAIR})

    if verbose:
        print(f"Loading {len(pairs)} Binance pairs ({start} -> {end or 'today'}) ...")
    opens, closes, missing = price_data.load_price_panel(
        pairs, start=start, end=end, use_cache=use_cache, verbose=False
    )
    if closes.empty:
        raise RuntimeError("No price data loaded; cannot backtest.")
    if verbose and missing:
        print(f"  {len(missing)} pairs had no Binance data and are skipped.")

    # Align panels on a common date index.
    common = closes.index.intersection(opens.index)
    closes, opens = closes.loc[common], opens.loc[common]

    fear_greed = price_data.load_fear_greed(start=start, end=end, use_cache=use_cache)

    engine = Backtester(spec, opens, closes, narratives, fear_greed, eligible_pairs)
    result = engine.run()
    return result


def _write_outputs(result: BacktestResult, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    result.equity.to_frame().assign(
        ret=result.returns, drawdown=result.equity / result.equity.cummax() - 1.0
    ).to_csv(out_dir / "equity.csv", index_label="date")
    result.trades.to_csv(out_dir / "trades.csv", index=False)
    result.weights.to_csv(out_dir / "weights.csv", index_label="date")
    result.rotation.drop(columns=["detail"], errors="ignore").to_csv(
        out_dir / "rotation.csv", index=False
    )
    (out_dir / "metrics.json").write_text(json.dumps(result.metrics, indent=2) + "\n")


def _pct(x: float) -> str:
    return "  n/a" if x is None or not np.isfinite(x) else f"{x * 100:+.2f}%"


def _num(x: float) -> str:
    return "  n/a" if x is None or not np.isfinite(x) else f"{x:.2f}"


def _print_summary(result: BacktestResult) -> None:
    m = result.metrics
    eq = result.equity
    print("\n" + "=" * 64)
    print(f"NarrativeAlpha backtest  ({eq.index.min().date()} -> {eq.index.max().date()}, "
          f"{m.get('n_days', 0)} days)")
    print("=" * 64)
    print(f"  Total return     {_pct(m.get('total_return')):>10}")
    print(f"  CAGR             {_pct(m.get('cagr')):>10}")
    print(f"  Ann. volatility  {_pct(m.get('ann_vol')):>10}")
    print(f"  Sharpe           {_num(m.get('sharpe')):>10}")
    print(f"  Sortino          {_num(m.get('sortino')):>10}")
    print(f"  Max drawdown     {_pct(m.get('max_drawdown')):>10}")
    print(f"  Calmar           {_num(m.get('calmar')):>10}")
    print(f"  Exposure         {_pct(m.get('exposure')):>10}")
    print(f"  Hit rate         {_pct(m.get('hit_rate')):>10}")
    print(f"  Rebalances       {m.get('n_rebalances', 0):>10}")
    print(f"  Total turnover   {_num(m.get('total_turnover')):>10}")

    bench = result.benchmarks.dropna(how="all")
    if not bench.empty:
        print("\n  Benchmarks (total return):")
        for col in bench.columns:
            series = bench[col].dropna()
            if not series.empty:
                print(f"    {col:<14} {_pct(series.iloc[-1] / series.iloc[0] - 1.0):>10}")

    print(f"\n  Trade log: {len(result.trades)} fills "
          f"across {result.trades['date'].nunique() if not result.trades.empty else 0} rebalances.")
    if not result.rotation.empty:
        print("  Recent rotations:")
        for _, row in result.rotation.tail(4).iterrows():
            print(f"    {pd.Timestamp(row['date']).date()}  "
                  f"{'RISK-OFF ' if row['risk_off'] else ''}"
                  f"held {row['n_held']}: {row['narratives']}")

    nan_count = int(result.equity.isna().sum())
    print(f"\n  Sanity: equity NaNs = {nan_count}  "
          f"(look-ahead-free: signal@close(d) -> fill@open(d+1))")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deterministically backtest a NarrativeAlpha strategy_spec.json."
    )
    parser.add_argument("spec", help="Path to strategy_spec.json")
    parser.add_argument("--start", default=price_data._DEFAULT_START)
    parser.add_argument("--end", default=None)
    parser.add_argument("--no-cache", action="store_true", help="Bypass the on-disk price cache.")
    parser.add_argument("--out", default=None, help="Directory to write equity/trades/metrics CSVs.")
    args = parser.parse_args(argv)

    result = run_backtest(
        args.spec, start=args.start, end=args.end, use_cache=not args.no_cache
    )
    _print_summary(result)

    if args.out:
        out_dir = Path(args.out)
        _write_outputs(result, out_dir)
        print(f"\nWrote equity/trades/weights/rotation/metrics -> {out_dir}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
