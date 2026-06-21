"""NarrativeAlpha reporting & validation layer (Phase 5).

Turns a :class:`~scripts.backtest.BacktestResult` into a credible, legible
report: an equity-curve PNG, a full metrics table (return, Sharpe, Sortino,
max DD, Calmar, turnover, hit rate), a rotation timeline (which narratives were
held when), benchmark comparisons (BTC HODL, equal-weight universe, no-rotation
baseline), an in-sample vs out-of-sample walk-forward split, and a
cost-sensitivity table.

Why these pieces (PRD sec.8): the strategy must beat the obvious passive
alternatives and survive higher costs, and the headline numbers must hold on
data the rules were *not* eyeballed against. The walk-forward split reports the
same fixed-default strategy on an early ("in-sample") and a later ("OOS") window
separately so a reader can see the edge is not a single-regime artifact.

CLI::

    python scripts/report.py examples/strategy_spec.example.json
    python scripts/report.py spec.json --out examples --name backtest_report.example
"""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

import matplotlib

matplotlib.use("Agg")  # headless: render straight to PNG, no display needed
import matplotlib.pyplot as plt  # noqa: E402

try:  # Allow both ``python scripts/report.py`` and ``import``.
    from backtest import (
        BacktestResult,
        _TRADING_DAYS,
        compute_metrics,
        run_backtest,
    )
    import data as price_data
except ImportError:  # pragma: no cover - package-style import
    from scripts.backtest import (
        BacktestResult,
        _TRADING_DAYS,
        compute_metrics,
        run_backtest,
    )
    from scripts import data as price_data


_REPO_ROOT = Path(__file__).resolve().parent.parent

# Friendly labels for the benchmark columns the engine emits.
_BENCH_LABELS = {
    "btc_hodl": "BTC buy & hold",
    "ew_universe": "Equal-weight universe",
    "no_rotation": "All narratives (no rotation)",
}

# Cost multipliers for the sensitivity sweep (1.0 = the spec's own fee/slippage).
_COST_MULTIPLIERS = (0.0, 0.5, 1.0, 2.0, 3.0)


# ============================================================================ #
# Metric helpers                                                               #
# ============================================================================ #
def _series_metrics(equity: pd.Series) -> Dict[str, float]:
    """Total return / CAGR / Sharpe / max-DD for a bare equity (benchmark) line."""
    eq = equity.dropna()
    if eq.empty or eq.iloc[0] <= 0:
        return {}
    rets = eq.pct_change(fill_method=None).fillna(0.0)
    years = len(eq) / _TRADING_DAYS
    cagr = float(eq.iloc[-1] ** (1.0 / years) - 1.0) if years > 0 and eq.iloc[-1] > 0 else float("nan")
    vol = float(rets.std(ddof=0) * np.sqrt(_TRADING_DAYS))
    sharpe = float(rets.mean() * _TRADING_DAYS / vol) if vol > 0 else float("nan")
    max_dd = float((eq / eq.cummax() - 1.0).min())
    return {
        "total_return": float(eq.iloc[-1] / eq.iloc[0] - 1.0),
        "cagr": cagr,
        "sharpe": sharpe,
        "max_drawdown": max_dd,
    }


def _window_metrics(result: BacktestResult, mask: pd.Series) -> Dict[str, float]:
    """Re-run the strategy metrics over a date sub-window (re-based to 1.0)."""
    eq = result.equity[mask]
    if eq.empty:
        return {}
    eq = eq / eq.iloc[0]
    rets = result.returns[mask]
    weights = result.weights[mask]
    trades = result.trades
    if not trades.empty:
        idx = eq.index
        trades = trades[(trades["date"] >= idx.min()) & (trades["date"] <= idx.max())]
    return compute_metrics(eq, rets, trades, weights)


def walk_forward(
    result: BacktestResult, split: float = 0.6
) -> Tuple[pd.Timestamp, Dict[str, float], Dict[str, float]]:
    """Split the equity history into in-sample / out-of-sample windows.

    Defaults are fixed (never fit on the data), so this is an *honesty* check, not
    a tuning loop: it shows the same strategy's metrics on an earlier and a later
    slice so the edge isn't a single-regime artifact. ``split`` is the in-sample
    fraction (0.6 -> first 60% in-sample, last 40% out-of-sample).
    """
    idx = result.equity.index
    if len(idx) < 4:
        return idx[0], {}, {}
    cut = idx[int(len(idx) * split)]
    is_mask = idx <= cut
    oos_mask = idx > cut
    return cut, _window_metrics(result, is_mask), _window_metrics(result, oos_mask)


def cost_sensitivity(
    spec_path: str | Path,
    spec: Dict[str, Any],
    start: str,
    end: Optional[str],
    multipliers: Tuple[float, ...] = _COST_MULTIPLIERS,
) -> pd.DataFrame:
    """Re-run the backtest at several fee/slippage levels (price cache makes this
    cheap) to show the edge survives realistic trading frictions.
    """
    base_fee = float(spec.get("costs", {}).get("fee_bps", 10.0))
    base_slip = float(spec.get("costs", {}).get("slippage_bps", 5.0))
    rows: List[Dict[str, Any]] = []
    for mult in multipliers:
        variant = copy.deepcopy(spec)
        variant.setdefault("costs", {})
        variant["costs"]["fee_bps"] = base_fee * mult
        variant["costs"]["slippage_bps"] = base_slip * mult
        res = run_backtest(
            spec_path, start=start, end=end, use_cache=True, verbose=False, spec=variant
        )
        m = res.metrics
        rows.append(
            {
                "cost_multiple": mult,
                "fee_bps": round(base_fee * mult, 2),
                "slippage_bps": round(base_slip * mult, 2),
                "total_return": m.get("total_return", float("nan")),
                "cagr": m.get("cagr", float("nan")),
                "sharpe": m.get("sharpe", float("nan")),
                "max_drawdown": m.get("max_drawdown", float("nan")),
                "total_cost": m.get("total_cost", float("nan")),
            }
        )
    return pd.DataFrame(rows)


# ============================================================================ #
# Chart                                                                         #
# ============================================================================ #
def _strategy_label(result: BacktestResult) -> str:
    return str(result.spec.get("name") or "Strategy")


def plot_equity(result: BacktestResult, out_png: Path, title: str) -> Path:
    """Equity curve (strategy vs benchmarks, log scale) over a drawdown panel."""
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig, (ax_eq, ax_dd) = plt.subplots(
        2, 1, figsize=(11, 7), sharex=True, gridspec_kw={"height_ratios": [3, 1]}
    )

    eq = result.equity
    ax_eq.plot(eq.index, eq.values, label=_strategy_label(result), color="#0b7285", linewidth=2.0)
    for col in result.benchmarks.columns:
        series = result.benchmarks[col].dropna()
        if series.empty:
            continue
        ax_eq.plot(
            series.index,
            series.values,
            label=_BENCH_LABELS.get(col, col),
            linewidth=1.1,
            alpha=0.8,
        )
    ax_eq.set_yscale("log")
    ax_eq.set_ylabel("Growth of $1 (log)")
    ax_eq.set_title(title)
    ax_eq.legend(loc="upper left", fontsize=9)
    ax_eq.grid(True, which="both", alpha=0.2)

    drawdown = eq / eq.cummax() - 1.0
    ax_dd.fill_between(drawdown.index, drawdown.values, 0.0, color="#c92a2a", alpha=0.35)
    ax_dd.set_ylabel("Drawdown")
    ax_dd.grid(True, alpha=0.2)
    ax_dd.yaxis.set_major_formatter(plt.FuncFormatter(lambda y, _: f"{y * 100:.0f}%"))

    fig.tight_layout()
    fig.savefig(out_png, dpi=120)
    plt.close(fig)
    return out_png


# ============================================================================ #
# Markdown rendering                                                            #
# ============================================================================ #
def _fmt_pct(x: Any) -> str:
    return "n/a" if x is None or not np.isfinite(x) else f"{x * 100:+.2f}%"


def _fmt_num(x: Any) -> str:
    return "n/a" if x is None or not np.isfinite(x) else f"{x:.2f}"


def _metrics_table(m: Dict[str, float]) -> str:
    rows = [
        ("Total return", _fmt_pct(m.get("total_return"))),
        ("CAGR", _fmt_pct(m.get("cagr"))),
        ("Annualized volatility", _fmt_pct(m.get("ann_vol"))),
        ("Sharpe", _fmt_num(m.get("sharpe"))),
        ("Sortino", _fmt_num(m.get("sortino"))),
        ("Max drawdown", _fmt_pct(m.get("max_drawdown"))),
        ("Calmar", _fmt_num(m.get("calmar"))),
        ("Exposure (time in market)", _fmt_pct(m.get("exposure"))),
        ("Hit rate (invested days)", _fmt_pct(m.get("hit_rate"))),
        ("Rebalances", str(m.get("n_rebalances", 0))),
        ("Total turnover", _fmt_num(m.get("total_turnover"))),
        ("Total cost", _fmt_pct(m.get("total_cost"))),
    ]
    lines = ["| Metric | Value |", "| --- | ---: |"]
    lines += [f"| {k} | {v} |" for k, v in rows]
    return "\n".join(lines)


def _benchmark_table(result: BacktestResult) -> str:
    lines = [
        "| Strategy / benchmark | Total return | CAGR | Sharpe | Max DD |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    sm = result.metrics
    lines.append(
        f"| **{_strategy_label(result)}** | {_fmt_pct(sm.get('total_return'))} | "
        f"{_fmt_pct(sm.get('cagr'))} | {_fmt_num(sm.get('sharpe'))} | "
        f"{_fmt_pct(sm.get('max_drawdown'))} |"
    )
    for col in result.benchmarks.columns:
        bm = _series_metrics(result.benchmarks[col])
        if not bm:
            continue
        lines.append(
            f"| {_BENCH_LABELS.get(col, col)} | {_fmt_pct(bm.get('total_return'))} | "
            f"{_fmt_pct(bm.get('cagr'))} | {_fmt_num(bm.get('sharpe'))} | "
            f"{_fmt_pct(bm.get('max_drawdown'))} |"
        )
    return "\n".join(lines)


def _walk_forward_table(
    cut: pd.Timestamp, is_m: Dict[str, float], oos_m: Dict[str, float]
) -> str:
    def col(m: Dict[str, float]) -> List[str]:
        return [
            _fmt_pct(m.get("total_return")),
            _fmt_pct(m.get("cagr")),
            _fmt_num(m.get("sharpe")),
            _fmt_num(m.get("sortino")),
            _fmt_pct(m.get("max_drawdown")),
            _fmt_num(m.get("calmar")),
        ]

    metrics = ["Total return", "CAGR", "Sharpe", "Sortino", "Max drawdown", "Calmar"]
    is_vals, oos_vals = col(is_m), col(oos_m)
    lines = [
        f"Split at **{pd.Timestamp(cut).date()}** "
        f"(in-sample: start → split, out-of-sample: split → end).",
        "",
        "| Metric | In-sample | Out-of-sample |",
        "| --- | ---: | ---: |",
    ]
    lines += [f"| {metrics[i]} | {is_vals[i]} | {oos_vals[i]} |" for i in range(len(metrics))]
    return "\n".join(lines)


def _cost_table(df: pd.DataFrame) -> str:
    lines = [
        "| Cost x | Fee bps | Slippage bps | Total return | CAGR | Sharpe | Max DD |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for _, r in df.iterrows():
        lines.append(
            f"| {r['cost_multiple']:.1f} | {r['fee_bps']:.1f} | {r['slippage_bps']:.1f} | "
            f"{_fmt_pct(r['total_return'])} | {_fmt_pct(r['cagr'])} | "
            f"{_fmt_num(r['sharpe'])} | {_fmt_pct(r['max_drawdown'])} |"
        )
    return "\n".join(lines)


def _rotation_timeline(result: BacktestResult, max_rows: int = 40) -> str:
    rot = result.rotation
    if rot.empty:
        return "_No rebalances recorded._"
    held_col = "Holdings" if _is_momentum(result) else "Narratives held"
    lines = [
        f"| Date | State | # held | {held_col} |",
        "| --- | --- | ---: | --- |",
    ]
    shown = rot.tail(max_rows) if len(rot) > max_rows else rot
    if len(rot) > max_rows:
        lines.append(f"| … | … | … | _(showing last {max_rows} of {len(rot)} rebalances)_ |")
    for _, row in shown.iterrows():
        if row.get("halted"):
            state = "HALTED (max-DD)"
        elif row.get("risk_off"):
            state = "RISK-OFF → cash"
        else:
            state = "invested"
        lines.append(
            f"| {pd.Timestamp(row['date']).date()} | {state} | {int(row['n_held'])} | "
            f"{row['narratives']} |"
        )
    return "\n".join(lines)


def _is_momentum(result: BacktestResult) -> bool:
    return str(result.spec.get("method", "")) == "cross_sectional_momentum"


def _exposure_breakdown(result: BacktestResult, top: int = 12) -> Tuple[str, str]:
    """Most-held names across rebalances. Returns ``(heading, table)``.

    For the momentum book the rotation "detail" carries token symbols, so this is
    a holdings-frequency table; for narrative rotation it's exposure by narrative.
    """
    rot = result.rotation
    if rot.empty:
        return "", ""
    momentum = _is_momentum(result)
    unit = "token" if momentum else "narrative"
    counts: Dict[str, int] = {}
    total = 0
    for _, row in rot.iterrows():
        for m in row.get("detail", []) or []:
            name = m.get("narrative", "?")
            counts[name] = counts.get(name, 0) + 1
        total += 1
    if not counts:
        return "", ""
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:top]
    lines = [
        f"| {unit.capitalize()} | Rebalances held | Share of rebalances |",
        "| --- | ---: | ---: |",
    ]
    for name, c in ordered:
        lines.append(f"| {name} | {c} | {c / total * 100:.0f}% |")
    heading = (
        "Most-held names (top momentum picks)"
        if momentum
        else "Exposure by narrative"
    )
    return heading, "\n".join(lines)


def render_markdown(
    result: BacktestResult,
    chart_rel: str,
    walk: Tuple[pd.Timestamp, Dict[str, float], Dict[str, float]],
    cost_df: pd.DataFrame,
    spec_name: str,
) -> str:
    eq = result.equity
    start, end = eq.index.min().date(), eq.index.max().date()
    m = result.metrics
    cut, is_m, oos_m = walk
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    label = _strategy_label(result)
    description = str(result.spec.get("description") or "").strip()
    parts: List[str] = []
    parts.append(f"# NarrativeAlpha — Backtest Report\n")
    parts.append(
        f"**Strategy:** {label} (`{spec_name}`)  \n"
        f"**Window:** {start} → {end} ({m.get('n_days', 0)} trading days)  \n"
        f"**Generated:** {generated}\n"
    )
    blurb = description or (
        "Long/flat weekly rotation into the strongest CoinMarketCap narratives, "
        "expressed through the eligible BEP-20 universe."
    )
    parts.append(
        f"> {blurb} Signal at close(d), filled at open(d+1) — no look-ahead. "
        "Defaults are fixed (not fit to this data).\n"
    )

    headline = (
        f"{label} returned **{_fmt_pct(m.get('total_return'))}** "
        f"(CAGR {_fmt_pct(m.get('cagr'))}) at a max drawdown of "
        f"**{_fmt_pct(m.get('max_drawdown'))}** "
        f"(Sharpe {_fmt_num(m.get('sharpe'))}, Calmar {_fmt_num(m.get('calmar'))}), "
        f"with only {_fmt_pct(m.get('exposure'))} time in market."
    )
    parts.append(f"## TL;DR\n\n{headline}\n")

    parts.append(f"## Equity curve\n\n![Equity curve]({chart_rel})\n")

    parts.append("## Performance metrics\n\n" + _metrics_table(m) + "\n")

    parts.append(
        "## Benchmarks\n\n"
        "Compared against BTC buy & hold, the passive equal-weight universe, and a "
        "no-rotation baseline (holding every narrative basket all the time) — the "
        "latter isolates the value added by active selection.\n\n"
        + _benchmark_table(result)
        + "\n"
    )

    parts.append(
        "## Walk-forward validation (in-sample vs out-of-sample)\n\n"
        + _walk_forward_table(cut, is_m, oos_m)
        + "\n"
    )

    parts.append(
        "## Cost sensitivity\n\n"
        "Re-run at multiples of the spec's fee + slippage to confirm the edge "
        "survives realistic frictions (1.0× is the spec's own assumption).\n\n"
        + _cost_table(cost_df)
        + "\n"
    )

    exp_heading, exp_table = _exposure_breakdown(result)
    if exp_table:
        parts.append(f"## {exp_heading}\n\n" + exp_table + "\n")

    parts.append("## Rotation timeline\n\n" + _rotation_timeline(result) + "\n")

    parts.append(
        "---\n\n_Reproduce: `python scripts/report.py "
        f"{spec_name}`. Past performance does not guarantee future results._\n"
    )
    return "\n".join(parts)


# ============================================================================ #
# Orchestration                                                                #
# ============================================================================ #
def generate_report(
    spec_path: str | Path,
    out_dir: str | Path = "examples",
    name: str = "backtest_report",
    start: str = price_data._DEFAULT_START,
    end: Optional[str] = None,
    split: float = 0.6,
    result: Optional[BacktestResult] = None,
    verbose: bool = True,
) -> Tuple[Path, Path]:
    """Run the full Phase-5 report pipeline and write Markdown + PNG.

    Returns ``(markdown_path, chart_path)``.
    """
    spec_path = Path(spec_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if result is None:
        if verbose:
            print(f"Running backtest for {spec_path.name} ...")
        result = run_backtest(spec_path, start=start, end=end, verbose=verbose)

    chart_path = out_dir / f"{name}.png"
    if verbose:
        print("Rendering equity curve ...")
    plot_equity(result, chart_path, title=f"NarrativeAlpha — {spec_path.stem}")

    if verbose:
        print("Walk-forward split ...")
    walk = walk_forward(result, split=split)

    if verbose:
        print(f"Cost-sensitivity sweep ({len(_COST_MULTIPLIERS)} runs) ...")
    cost_df = cost_sensitivity(spec_path, result.spec or {}, start, end)

    md = render_markdown(result, chart_path.name, walk, cost_df, spec_path.name)
    md_path = out_dir / f"{name}.md"
    md_path.write_text(md)
    if verbose:
        print(f"Wrote report  -> {md_path}")
        print(f"Wrote chart   -> {chart_path}")
    return md_path, chart_path


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render a NarrativeAlpha backtest report (metrics, benchmarks, "
        "walk-forward, cost-sensitivity, rotation timeline)."
    )
    parser.add_argument("spec", help="Path to strategy_spec.json")
    parser.add_argument("--out", default="examples", help="Output directory.")
    parser.add_argument("--name", default="backtest_report", help="Output file stem.")
    parser.add_argument("--start", default=price_data._DEFAULT_START)
    parser.add_argument("--end", default=None)
    parser.add_argument(
        "--split", type=float, default=0.6, help="In-sample fraction for walk-forward."
    )
    args = parser.parse_args(argv)

    generate_report(
        args.spec,
        out_dir=args.out,
        name=args.name,
        start=args.start,
        end=args.end,
        split=args.split,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
