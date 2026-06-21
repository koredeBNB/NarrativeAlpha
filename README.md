# NarrativeAlpha

> A CoinMarketCap **Strategy Skill** (BNB Hack — Track 2) that turns CMC's
> narrative/sector data into a typed, **backtestable** crypto rotation strategy.

NarrativeAlpha builds long/flat, risk-managed crypto strategies over the
**official BEP-20 universe** (145 tokens; 75 priceable via Binance spot) and
proves them out with a deterministic, look-ahead-free backtest + report. The
recommended (submission) strategy is **cross-sectional risk-adjusted momentum**,
inverse-vol sized to a vol target and gated by a **BTC 200-DMA trend regime** +
max-drawdown halt; the engine also supports CMC **narrative/sector rotation**.
The skill emits a schema-valid `strategy_spec.json` for either method.

See [`findings.md`](findings.md) for the full research arc (why narrative
rotation was demoted, the look-ahead audit, universe correction, and tuning).

## What & why

Sector momentum / rotation is one of the oldest, most robust factors in equities.
It is under-exploited in crypto and effectively **impossible to build without
CMC's narrative taxonomy**. NarrativeAlpha ports that factor to crypto using a
signal only CoinMarketCap publishes, then proves the mechanics on real price
history. The emitted spec is drop-in for a Track 1 execution agent.

See [`PRD.md`](PRD.md) for the full design rationale and [`SKILL.md`](SKILL.md)
for the LLM-Skill contract and procedure.

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

Then run the two documented commands:

```bash
# A) Generate a narrative-rotation spec from live CMC narratives
python scripts/build_spec.py            # -> writes strategy_spec.json

# B) Backtest the recommended (momentum) strategy and render the report
python scripts/report.py examples/strategy_spec.momentum.example.json --start 2021-01-01
# -> examples/backtest_report.{md,png}
```

Want just the raw equity series + trade log?

```bash
python scripts/backtest.py examples/strategy_spec.momentum.example.json \
  --start 2021-01-01 --out runs/momentum_v1
```

> Start from **2021** so BTC's 200-DMA is live on 2022-01-01 — otherwise the
> warmup forces cash through the 2022 bear and flatters results (`findings.md` §7).

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

## Repository layout

```
narrativealpha/
├── README.md                      # this file
├── SKILL.md                       # the LLM Skill: frontmatter + procedure
├── PRD.md                         # design rationale
├── phases.md                      # build phases
├── requirements.txt
├── .env.example                   # CMC_API_KEY=...
├── scripts/
│   ├── cmc_client.py              # MCP/REST client + string normalizer
│   ├── build_spec.py              # narratives -> strategy_spec.json (validated)
│   ├── universe.py                # BEP-20 universe + narrative->token mapping
│   ├── data.py                    # Binance klines + Fear & Greed loader (cached)
│   ├── backtest.py                # deterministic backtest of a spec (both methods)
│   └── report.py                  # equity curve, metrics, benchmarks, timeline
├── reference/
│   ├── strategy_schema.json       # JSON Schema for the spec
│   ├── bep20_universe.csv         # authoritative BEP-20 list (symbol + BSC address)
│   ├── eligible_tokens.json       # resolved universe (145 tokens; 75 priceable)
│   └── narrative_token_map.json   # frozen narrative -> constituents (no look-ahead)
├── findings.md                    # research arc, look-ahead audit, tuning
└── examples/
    ├── strategy_spec.momentum.example.json   # recommended (submission) strategy
    ├── strategy_spec.example.json            # narrative rotation
    ├── backtest_report.md
    └── backtest_report.png
```

## How it works

The pipeline deliberately separates two layers:

- **Live signal layer** — CMC `trending_crypto_narratives` drives the *current*
  spec. The CMC-exclusive fields (volume-weighted relative perf, social author
  count) are the parts that cannot be reconstructed, so they are the live overlay.
- **Historical price layer** — a **frozen** `narrative_token_map.json` + free
  Binance daily klines rebuild each narrative's basket index, so the rotation
  rules backtest point-in-time and **look-ahead-free**. Fear & Greed history
  powers the risk-off gate.

Backtest discipline: signal at `close(d)`, fill at `open(d+1)`; biweekly
rebalance; fee + slippage on turnover; BTC 200-DMA trend gate + max-drawdown
halt; fixed defaults (not fit to test data); long-only.

## Example results

See [`examples/backtest_report.md`](examples/backtest_report.md) for a full
rendered report (metrics, benchmarks, walk-forward, cost-sensitivity, rotation
timeline) and equity curve. Headline (2021 full cycle): **+56% return at −20%
max drawdown vs BTC +117% at −77%** — i.e. far smoother risk-adjusted equity, not
a raw-return beat. See [`findings.md`](findings.md) for the honest framing.

## Notes

- CMC OHLCV history is gated (403) on the Agent Hub plan; Binance klines are the
  historical price source. Upgrading the plan swaps in `ohlcv/historical` with
  zero spec changes.
- Track 2 deliverable: a backtestable strategy spec — **no live execution**.
- Not financial advice. Past performance does not guarantee future results.
