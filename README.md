# NarrativeAlpha

> A CoinMarketCap **Strategy Skill** (BNB Hack — Track 2) that turns CMC's live
> narrative data into a typed, **backtestable** crypto trading strategy.

**Problem:** traders chase crypto narratives emotionally, buy tops, and blow up in
drawdowns. **Solution:** NarrativeAlpha converts CMC's live narrative signal into a
backtested, risk-managed strategy spec — it picks the hot themes, buys the real
winners within them, and steps to cash in bear markets.

The result is one unified strategy over the **official BEP-20 universe** (145
tokens; 75 priceable via Binance spot), proven out with a deterministic,
look-ahead-free backtest + report, and packaged as an installable
[`SKILL.md`](SKILL.md).

## The strategy — narrative-fed cross-sectional momentum

A single long/flat, risk-managed book. Each rebalance runs top-to-bottom:

1. **CMC narratives set the theme** — rank `trending_crypto_narratives` by
   volume-weighted performance vs the total market; the top narratives are this
   period's hot themes.
2. **Momentum picks the winners** — score every priceable token by risk-adjusted
   momentum (blended 20/40/60-day returns ÷ volatility, skipping the last 7d),
   then **boost the score of tokens sitting in a leading narrative**
   (`narrative_tilt`). This is how the CMC signal *directly steers selection* — a
   tilt, not a hard filter, so momentum still picks winners within the theme.
3. **Select & size** — hold the top-N names, inverse-vol weighted to a portfolio
   vol target, capped per name, with a no-trade band to curb churn.
4. **Risk gates decide whether to hold at all** — BTC 200-DMA trend regime
   (cash below) + Fear & Greed extreme override + a sticky max-drawdown halt.

Signal at `close(d)`, filled at `open(d+1)` — **deterministic and
look-ahead-free**. The engine also supports a pure `narrative_rotation` method
(rotate straight into narrative leaders, no momentum) as a simpler CMC-only
baseline.

## Quickstart

Requires Python 3.9+ and a CoinMarketCap API key
([get one](https://pro.coinmarketcap.com)).

```bash
# 1. Install
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 2. Configure your key
cp .env.example .env
# edit .env and set CMC_API_KEY=...
```

Then two commands do everything:

```bash
# A) Generate the narrative-fed momentum spec live from CMC
python scripts/build_spec.py --method momentum -o strategy_spec.json

# B) Backtest it and render the report
python scripts/report.py strategy_spec.json --start 2021-01-01
# -> examples/backtest_report.{md,png}
```

Want just the raw equity series + trade log?

```bash
python scripts/backtest.py strategy_spec.json --start 2021-01-01 --out runs/momentum_v1
```

Useful flags: `--method narrative` (pure CMC rotation), `--rebuild-map` (re-pull
the narrative→token map live), `--top-n N` (narratives feeding the tilt).

> Start from **2021** so BTC's 200-DMA is live on 2022-01-01 — otherwise the
> warmup forces cash through the 2022 bear and flatters results.

## Results

Full cycle **2021-01-01 → 2026-06-20**, 75 priceable BEP-20 tokens, real Binance
prices, costs (10 bps fee + 5 bps slippage) included.

| Strategy / benchmark | Total return | Sharpe | Max drawdown |
| --- | ---: | ---: | ---: |
| **NarrativeAlpha (narrative-fed momentum)** | **+69.3%** | **0.71** | **−19.8%** |
| BTC buy & hold | +116.6% | 0.53 | −76.6% |
| Equal-weight universe | +165.5% | 0.62 | −78.8% |

This is a **risk-managed / drawdown-control** strategy: it trails BTC's raw
return over a full cycle but at roughly **1/4 of BTC's max drawdown**, with a
higher Sharpe and far smoother equity. Adding the CMC narrative tilt lifted the
book from +56.5% / Sharpe 0.64 / −20.3% (momentum-only) to the numbers above —
**better on return, Sharpe, and drawdown** — direct evidence the CMC signal adds
edge. Validated out-of-sample (walk-forward) and across 0×–3× cost levels.

See [`examples/backtest_report.md`](examples/backtest_report.md) for the full
rendered report (metrics, benchmarks, walk-forward, cost-sensitivity, rotation
timeline) and equity curve.

![Equity curve](examples/backtest_report.png)

## Tool-usage map (CMC Agent Hub)

| Role | CMC tool | Used in |
| --- | --- | --- |
| Core signal | `trending_crypto_narratives` | `build_spec.py` |
| ID resolution | `search_cryptos` | `universe.py` |
| Universe / sector map | `get_crypto_info`, `v1/cryptocurrency/categories` | `universe.py` |
| Entry timing | `get_crypto_technical_analysis` | `build_spec.py` (spec params) |
| Risk-off filter | `get_global_metrics_latest` + Fear & Greed historical | `backtest.py` |

The CMC client (`scripts/cmc_client.py`) speaks MCP JSON-RPC to
`https://mcp.coinmarketcap.com/mcp` (header `X-CMC-MCP-API-KEY`) with a REST
fallback (`X-CMC_PRO_API_KEY`), and normalizes display-formatted payloads
(`"1.41 T"` → `1.41e12`, `"+2.39%"` → `0.0239`).

## How it works

The pipeline deliberately separates two layers:

- **Live signal layer** — CMC `trending_crypto_narratives` drives the *current*
  spec. The CMC-exclusive fields (volume-weighted relative perf, social author
  count) cannot be reconstructed historically, so they are the live overlay.
- **Historical price layer** — a **frozen** `narrative_token_map.json` + free
  Binance daily klines rebuild each narrative's basket index, so the rules
  backtest point-in-time and **look-ahead-free**. Fear & Greed history powers the
  risk-off gate.

Backtest discipline: signal at `close(d)`, fill at `open(d+1)`; biweekly
rebalance; fee + slippage on turnover; BTC 200-DMA trend gate + max-drawdown
halt; fixed defaults (not fit to test data); long-only.

## Repository layout

```
NarrativeAlpha/
├── README.md                      # this file
├── SKILL.md                       # the LLM Skill: frontmatter + procedure
├── requirements.txt
├── .env.example                   # CMC_API_KEY=...
├── scripts/
│   ├── cmc_client.py              # MCP/REST client + string normalizer
│   ├── build_spec.py              # CMC narratives -> strategy_spec.json (validated)
│   ├── universe.py                # BEP-20 universe + narrative->token mapping
│   ├── data.py                    # Binance klines + Fear & Greed loader (cached)
│   ├── backtest.py                # deterministic backtest of a spec (both methods)
│   └── report.py                  # equity curve, metrics, benchmarks, timeline
├── reference/
│   ├── strategy_schema.json       # JSON Schema for the spec
│   ├── bep20_universe.csv         # authoritative BEP-20 list (symbol + BSC address)
│   ├── eligible_tokens.json       # resolved universe (145 tokens; 75 priceable)
│   └── narrative_token_map.json   # frozen narrative -> constituents (no look-ahead)
├── examples/
│   ├── strategy_spec.momentum.example.json   # narrative-fed momentum (recommended)
│   ├── strategy_spec.example.json            # pure narrative rotation
│   ├── backtest_report.md
│   └── backtest_report.png
└── runs/                          # committed example backtest outputs (CSV/JSON)
```

## Notes

- CMC OHLCV history is gated (403) on the Agent Hub plan; Binance klines are the
  historical price source. Upgrading the plan swaps in `ohlcv/historical` with
  zero spec changes.
- Track 2 deliverable: a backtestable strategy spec — **no live execution**.
- Not financial advice. Past performance does not guarantee future results.
