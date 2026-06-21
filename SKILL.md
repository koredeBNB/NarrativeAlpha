---
name: NarrativeAlpha
description: >-
  Generate a backtestable crypto strategy spec from CoinMarketCap data over the
  official BEP-20 token universe. Use when the user wants a typed, schema-valid
  strategy_spec.json and a deterministic, look-ahead-free backtest + report
  (equity curve, metrics, benchmarks, rotation timeline). Supports two methods:
  cross-sectional risk-adjusted momentum (the recommended, trend-gated book) and
  CMC narrative/sector rotation.
---

# NarrativeAlpha — CMC Strategy Skill

NarrativeAlpha builds long/flat, risk-managed crypto strategies over the
**official BEP-20 universe** and proves them out with a reproducible,
look-ahead-free backtest. It supports two strategy `method`s in one spec/engine:

1. **`cross_sectional_momentum`** *(recommended / submission strategy)* — ranks
   every priceable token by risk-adjusted momentum (blended 20/40/60-day returns
   ÷ volatility, skipping the last 7d), **tilts the score toward tokens in the
   leading CMC narratives** (`momentum.narrative_tilt`, default 0.15), holds the
   top-N, sizes inverse-vol to a portfolio vol target, and gates exposure with a
   **BTC 200-DMA trend regime** + max-drawdown halt. The CMC `trending_crypto_narratives`
   signal feeds directly into selection as a tilt (not a hard filter); a small
   tilt (0.10–0.15) is the robust sweet spot, large tilts override momentum and
   degrade results. A risk-managed book: shallow drawdowns across the cycle.
2. **`narrative_rotation`** — ranks CMC's **`trending_crypto_narratives`** by
   volume-weighted performance vs the total market and rotates straight into the
   leaders' constituents. A pure-CMC variant of the signal.

The skill produces two artifacts:

1. `strategy_spec.json` — a typed, schema-validated description of the strategy
   (method, signal/momentum ranking, token selection, timing, risk gates, sizing,
   costs, cadence). Consumable by both this backtester and a Track 1 execution
   agent.
2. A **backtest report** — equity curve PNG + Markdown with metrics, benchmarks,
   walk-forward (in-sample vs out-of-sample), cost-sensitivity, and a rotation
   timeline (which tokens were held when).

The strategy reflects a full research arc: narrative rotation alone was a weak,
noisy signal, so it was rebuilt as risk-managed momentum and the CMC narrative
signal was fed back in as a score tilt (which measurably improved return, Sharpe,
and drawdown), alongside a universe correction and a look-ahead/bias audit.

## When to use

Trigger this skill when the user asks to:
- build a CMC narrative- or sector-rotation strategy,
- generate a backtestable strategy spec from CoinMarketCap market data, or
- backtest / report on an existing `strategy_spec.json`.

## Required setup

- Python 3.9+ and `pip install -r requirements.txt`.
- `CMC_API_KEY` in the environment (or in a `.env` file). Get one at
  <https://pro.coinmarketcap.com>. The key is used for the CMC MCP server header
  `X-CMC-MCP-API-KEY` and the REST fallback header `X-CMC_PRO_API_KEY`.
- Network access to `https://mcp.coinmarketcap.com/mcp` (signal) and Binance
  daily klines (historical prices for the backtest). Prices and Fear & Greed
  history are cached on disk under `.cache/`.

## Required CMC tools

| Role | CMC tool | Notes |
| --- | --- | --- |
| Core signal | `trending_crypto_narratives` | No args. Returns `headers`+`rows` with volume-weighted perf vs market (24h/7d/30d), social author count, and nested `topCoinList`. |
| ID resolution | `search_cryptos` | Symbol → numeric CMC id for the universe. |
| Universe / sector map | `get_crypto_info`, `v1/cryptocurrency/categories` | Token ↔ narrative mapping + tags. |
| Entry timing | `get_crypto_technical_analysis` | Per-token RSI for the overbought skip. |
| Risk-off filter | `get_global_metrics_latest` + Fear & Greed historical | Drives the to-cash gate. |

Payload caveat: CMC narrative values arrive as **display-formatted strings**
(`"1.41 T"`, `"+2.39%"`). `cmc_client.normalize()` converts them to floats.

## Procedure

Run from the repo root. The pipeline is split into a **live signal layer** (CMC
narratives, drives the current spec) and a **historical price layer** (Binance
klines + frozen narrative→token map, drives the backtest with no look-ahead).

1. **Resolve the universe.** `reference/eligible_tokens.json` holds the official
   **BEP-20 universe** (145 tokens; symbol + BSC contract address). Each is
   resolved to a Binance `*USDT` spot pair where one exists: **75 are priceable**
   via Binance and used by the backtest; the rest are DEX-only on BSC
   (`priceable: false`) and skipped (no CEX OHLCV). To rebuild from the
   authoritative list `reference/bep20_universe.csv`:
   `python scripts/universe.py build-bep20`.

2. **Build the strategy spec (live CMC signal).**

   ```bash
   python scripts/build_spec.py
   ```

   This pulls `trending_crypto_narratives`, normalizes the formatted strings,
   ranks narratives by volume-weighted relative strength (7d/30d) with a
   market-cap confirmation, applies the **exhaustion filter** (down-ranks
   late-stage blow-offs), maps each held narrative's `topCoinList` to the
   eligible BEP-20 subset, attaches default timing/risk/cost params, and writes a
   schema-valid `strategy_spec.json` (validated against
   `reference/strategy_schema.json`). Useful flags: `--top-n N`,
   `--rebuild-map`, `-o <path>`, `--example`.

3. **Backtest the spec (historical prices).**

   ```bash
   # Recommended submission strategy (cross-sectional momentum), honest full cycle
   python scripts/backtest.py examples/strategy_spec.momentum.example.json \
     --start 2021-01-01 --out runs/momentum_v1
   ```

   For `cross_sectional_momentum`: ranks the priceable universe by risk-adjusted
   momentum, holds top-N, inverse-vol sizes to the vol target, fills at next open,
   gates with the BTC 200-DMA regime + Fear & Greed extreme, applies the
   max-drawdown halt and fee/slippage on turnover. For `narrative_rotation`:
   reconstructs each narrative basket from constituent klines and rotates the
   leaders. Both return an equity series + trade log with no look-ahead and no
   NaNs. Start from **2021** so BTC's 200-DMA is live on 2022-01-01 (avoids a
   warmup free-pass through the 2022 bear).

4. **Render the report.**

   ```bash
   python scripts/report.py examples/strategy_spec.momentum.example.json \
     --start 2021-01-01
   ```

   Writes `examples/backtest_report.{md,png}`: equity curve + drawdown, metrics
   (return, Sharpe, Sortino, max DD, Calmar, turnover, hit rate), benchmarks (BTC
   HODL, equal-weight universe, no-rotation baseline), walk-forward
   in/out-of-sample, cost-sensitivity, and the rotation timeline.

## Outputs

- `strategy_spec.json` (root) — the primary deliverable; schema:
  `reference/strategy_schema.json`. Committed examples:
  `examples/strategy_spec.momentum.example.json` (recommended) and
  `examples/strategy_spec.example.json` (narrative rotation).
- `examples/backtest_report.md` + `examples/backtest_report.png` — committed
  example report and chart for the submission strategy.

## Notes & guardrails

- This is a **risk-managed / drawdown-control** strategy, not a "beat BTC on
  return" strategy. Over a full cycle it trails BTC buy-and-hold on raw return
  but with ~1/4 the max drawdown. Report the risk-adjusted framing honestly.
- CMC OHLCV history is gated (403) on the Agent Hub plan and narratives are
  live-only — hence the Binance reconstruction with a **frozen**
  `reference/narrative_token_map.json` to keep the backtest point-in-time.
- Defaults are fixed (not fit to test data). Long-only. This is a Track 2
  research artifact, not a live-execution agent.
