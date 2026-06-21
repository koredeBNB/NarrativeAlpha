"""NarrativeAlpha universe & narrative->token map (Phase 2).

Two jobs, both reproducible from live CMC + Binance data:

1. **Eligible universe** (``reference/eligible_tokens.json``): the tradable
   universe used everywhere downstream. Per the hackathon framing we take the
   top coins from CoinMarketCap's market-cap ranking and keep only those that
   resolve to a real Binance ``*USDT`` spot pair (so the backtest can price
   them from free Binance klines). We walk the ranking until we have
   ``TARGET_TOKENS`` (149) tradable names. Each entry carries
   ``symbol, cmc_id, name, slug, cmc_rank, binance_pair``.

2. **Narrative -> token map** (``reference/narrative_token_map.json``): a
   *frozen* snapshot mapping each live narrative to its eligible constituent
   tokens. The backtest reads this fixed map so it never looks ahead: the set
   of tokens a narrative "contains" is decided once, here, not re-derived from
   future data. Constituents come from each narrative's ``topCoinList`` and
   (when available) its matching CMC category membership, intersected with the
   eligible universe. Narratives with zero eligible tokens are dropped.

CLI::

    python scripts/universe.py build-tokens   # (re)write eligible_tokens.json
    python scripts/universe.py build-map       # (re)write narrative_token_map.json
    python scripts/universe.py                 # build both, then verify Phase 2

The bare invocation also runs the Phase 2 acceptance check: every eligible
token has a valid Binance ``*USDT`` pair, and every retained narrative maps to
at least one eligible token.
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:  # Allow both ``python scripts/universe.py`` and ``import``.
    from cmc_client import CMCClient, CMCError
except ImportError:  # pragma: no cover - import path when used as a package
    from scripts.cmc_client import CMCClient, CMCError

import requests


# ---------------------------------------------------------------- config -----
TARGET_TOKENS = 149            # size of the eligible universe (hackathon list)
LISTINGS_SCAN_LIMIT = 500      # how far down the CMC ranking we scan for pairs
CATEGORY_MEMBER_LIMIT = 100    # max members pulled per narrative category
BINANCE_QUOTE = "USDT"

_REPO_ROOT = Path(__file__).resolve().parent.parent
_REFERENCE_DIR = _REPO_ROOT / "reference"
ELIGIBLE_TOKENS_PATH = _REFERENCE_DIR / "eligible_tokens.json"
NARRATIVE_MAP_PATH = _REFERENCE_DIR / "narrative_token_map.json"
BEP20_CSV_PATH = _REFERENCE_DIR / "bep20_universe.csv"

BINANCE_EXCHANGE_INFO_URL = "https://api.binance.com/api/v3/exchangeInfo"

# Symbols where CoinMarketCap and Binance disagree on the ticker. Maps a CMC
# symbol -> Binance base asset. Kept tiny and explicit; extend if verification
# reports a well-known asset being dropped purely over a ticker mismatch.
_SYMBOL_ALIASES: Dict[str, str] = {}


# ---------------------------------------------------------- data sourcing ----
def fetch_cmc_listings(
    client: CMCClient, limit: int = LISTINGS_SCAN_LIMIT
) -> List[Dict[str, Any]]:
    """Return CMC coins ranked by market cap (id, name, symbol, slug, rank)."""
    payload = client.rest(
        "/v1/cryptocurrency/listings/latest",
        {"start": 1, "limit": limit, "sort": "market_cap"},
    )
    data = payload.get("data") if isinstance(payload, dict) else None
    if not data:
        raise CMCError("listings/latest returned no data")
    rows: List[Dict[str, Any]] = []
    for coin in data:
        rows.append(
            {
                "cmc_id": coin.get("id"),
                "name": coin.get("name"),
                "symbol": str(coin.get("symbol", "")).strip().upper(),
                "slug": coin.get("slug"),
                "cmc_rank": coin.get("cmc_rank"),
            }
        )
    rows.sort(key=lambda r: (r["cmc_rank"] is None, r["cmc_rank"]))
    return rows


def fetch_binance_usdt_pairs(timeout: int = 30) -> Dict[str, str]:
    """Map Binance base asset -> ``*USDT`` spot pair symbol (TRADING only)."""
    resp = requests.get(
        BINANCE_EXCHANGE_INFO_URL,
        headers={"User-Agent": "narrativealpha/0.1"},
        timeout=timeout,
    )
    resp.raise_for_status()
    info = resp.json()
    pairs: Dict[str, str] = {}
    for sym in info.get("symbols", []):
        if (
            sym.get("quoteAsset") == BINANCE_QUOTE
            and sym.get("status") == "TRADING"
            and sym.get("isSpotTradingAllowed", True)
        ):
            base = str(sym.get("baseAsset", "")).strip().upper()
            if base and base not in pairs:
                pairs[base] = sym.get("symbol")
    return pairs


def _binance_pair_for(symbol: str, binance_pairs: Dict[str, str]) -> Optional[str]:
    sym = symbol.strip().upper()
    if sym in binance_pairs:
        return binance_pairs[sym]
    alias = _SYMBOL_ALIASES.get(sym)
    if alias and alias in binance_pairs:
        return binance_pairs[alias]
    return None


# ------------------------------------------------------ eligible universe ----
def build_eligible_tokens(
    client: Optional[CMCClient] = None,
    target: int = TARGET_TOKENS,
    scan_limit: int = LISTINGS_SCAN_LIMIT,
) -> List[Dict[str, Any]]:
    """Build the eligible universe: top market-cap coins with Binance USDT pairs.

    Walks the CMC market-cap ranking and keeps each coin that has a valid
    Binance ``*USDT`` spot pair, stopping once ``target`` tradable tokens are
    collected. Coins without a pair (most stablecoins, wrapped/derivative
    tokens, illiquid names) are skipped so the universe is fully backtestable.
    """
    client = client or CMCClient()
    listings = fetch_cmc_listings(client, scan_limit)
    binance_pairs = fetch_binance_usdt_pairs()

    eligible: List[Dict[str, Any]] = []
    seen_symbols: set[str] = set()
    for coin in listings:
        symbol = coin["symbol"]
        if not symbol or symbol in seen_symbols:
            continue
        pair = _binance_pair_for(symbol, binance_pairs)
        if pair is None:
            continue
        seen_symbols.add(symbol)
        eligible.append(
            {
                "symbol": symbol,
                "cmc_id": coin["cmc_id"],
                "name": coin["name"],
                "slug": coin["slug"],
                "cmc_rank": coin["cmc_rank"],
                "binance_pair": pair,
            }
        )
        if len(eligible) >= target:
            break

    if len(eligible) < target:
        print(
            f"  warning: only {len(eligible)} tradable tokens found in the top "
            f"{scan_limit} by market cap (target {target}); "
            "raise scan_limit if you need the full list.",
            file=sys.stderr,
        )
    return eligible


def load_bep20_list(path: Path = BEP20_CSV_PATH) -> List[Dict[str, str]]:
    """Read the authoritative ``symbol,bsc_address`` BEP-20 universe CSV."""
    rows: List[Dict[str, str]] = []
    with open(path, newline="") as fh:
        for rec in csv.DictReader(fh):
            sym = (rec.get("symbol") or "").strip()
            addr = (rec.get("bsc_address") or "").strip()
            if sym:
                rows.append({"symbol": sym, "bsc_address": addr})
    return rows


def build_eligible_from_bep20(
    path: Path = BEP20_CSV_PATH,
) -> List[Dict[str, Any]]:
    """Build the eligible universe from the official BEP-20 contract list.

    The competition universe is defined by on-chain BEP-20 contracts; for the
    backtest we price each token from its Binance ``*USDT`` spot pair where one
    exists (the liquid majors). Tokens without a Binance spot pair are DEX-only
    on BSC and are kept in the list but flagged ``priceable=False`` (the backtest
    skips them, as they have no CEX OHLCV history).
    """
    tokens_in = load_bep20_list(path)
    binance_pairs = fetch_binance_usdt_pairs()
    out: List[Dict[str, Any]] = []
    for tok in tokens_in:
        pair = _binance_pair_for(tok["symbol"], binance_pairs)
        out.append(
            {
                "symbol": tok["symbol"],
                "bsc_address": tok["bsc_address"],
                "binance_pair": pair,
                "priceable": pair is not None,
            }
        )
    return out


def write_eligible_tokens(
    tokens: List[Dict[str, Any]],
    path: Path = ELIGIBLE_TOKENS_PATH,
    meta: Optional[Dict[str, Any]] = None,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    base_meta = {
        "description": "Eligible BEP-20/top-market-cap trading universe for "
        "NarrativeAlpha. Top CMC coins by market cap that resolve to a "
        "Binance *USDT spot pair.",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "coinmarketcap listings/latest x binance exchangeInfo",
        "count": len(tokens),
        "quote_asset": BINANCE_QUOTE,
    }
    if meta:
        base_meta.update(meta)
    doc = {"_meta": base_meta, "tokens": tokens}
    path.write_text(json.dumps(doc, indent=2) + "\n")
    return path


def load_eligible_tokens(path: Path = ELIGIBLE_TOKENS_PATH) -> List[Dict[str, Any]]:
    """Load the eligible universe written by :func:`write_eligible_tokens`."""
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Run `python scripts/universe.py build-tokens` first."
        )
    return json.loads(path.read_text()).get("tokens", [])


def _eligible_lookups(
    tokens: List[Dict[str, Any]],
) -> Tuple[Dict[str, Dict[str, Any]], Dict[int, Dict[str, Any]]]:
    by_symbol: Dict[str, Dict[str, Any]] = {}
    by_id: Dict[int, Dict[str, Any]] = {}
    for tok in tokens:
        sym = str(tok.get("symbol", "")).strip().upper()
        if sym:
            by_symbol.setdefault(sym, tok)
        cid = tok.get("cmc_id")
        if cid is not None:
            by_id.setdefault(int(cid), tok)
    return by_symbol, by_id


# -------------------------------------------------- narrative -> token map ---
def _category_index(client: CMCClient) -> Dict[str, str]:
    """Build a ``lower(category name) -> category id`` index from CMC."""
    payload = client.rest("/v1/cryptocurrency/categories")
    data = payload.get("data") if isinstance(payload, dict) else None
    index: Dict[str, str] = {}
    for cat in data or []:
        name = str(cat.get("name", "")).strip().lower()
        cid = cat.get("id")
        if name and cid:
            index.setdefault(name, cid)
    return index


def _category_members(
    client: CMCClient, category_id: str, limit: int = CATEGORY_MEMBER_LIMIT
) -> List[Dict[str, Any]]:
    """Return coins (id, symbol, name) belonging to a CMC category."""
    try:
        payload = client.rest(
            "/v1/cryptocurrency/category", {"id": category_id, "limit": limit}
        )
    except (requests.RequestException, CMCError):
        return []
    data = payload.get("data") if isinstance(payload, dict) else None
    coins = (data or {}).get("coins") if isinstance(data, dict) else None
    members: List[Dict[str, Any]] = []
    for coin in coins or []:
        members.append(
            {
                "cmc_id": coin.get("id"),
                "symbol": str(coin.get("symbol", "")).strip().upper(),
                "name": coin.get("name"),
            }
        )
    return members


def _as_int(value: Any) -> Any:
    """Coerce a whole-number float (e.g. ``1.0`` from the normalizer) to int."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _token_entry(eligible: Dict[str, Any], source: str, perf_7d: Any = None) -> Dict[str, Any]:
    entry = {
        "symbol": eligible["symbol"],
        "cmc_id": eligible["cmc_id"],
        "binance_pair": eligible["binance_pair"],
        "source": source,
    }
    if perf_7d is not None:
        entry["price_change_7d"] = perf_7d
    return entry


def map_narrative_to_tokens(
    record: Dict[str, Any],
    by_symbol: Dict[str, Dict[str, Any]],
    by_id: Dict[int, Dict[str, Any]],
    category_members: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Resolve one narrative's constituents to the eligible universe.

    ``topCoinList`` symbols are matched first (preferred source), then any
    category members are folded in (matched by CMC id, falling back to symbol).
    The returned list is de-duplicated by symbol and preserves discovery order.
    """
    resolved: Dict[str, Dict[str, Any]] = {}

    for coin in record.get("topCoinList") or []:
        if not isinstance(coin, dict):
            continue
        sym = str(coin.get("coinSymbol", "")).strip().upper()
        eligible = by_symbol.get(sym)
        if eligible and eligible["symbol"] not in resolved:
            resolved[eligible["symbol"]] = _token_entry(
                eligible, "topCoinList", coin.get("priceChangePercent7d")
            )

    for member in category_members or []:
        cid = member.get("cmc_id")
        eligible = by_id.get(int(cid)) if cid is not None else None
        if eligible is None:
            eligible = by_symbol.get(str(member.get("symbol", "")).strip().upper())
        if eligible and eligible["symbol"] not in resolved:
            resolved[eligible["symbol"]] = _token_entry(eligible, "category")

    return list(resolved.values())


def build_narrative_token_map(
    client: Optional[CMCClient] = None,
    eligible_tokens: Optional[List[Dict[str, Any]]] = None,
    expand_with_categories: bool = True,
) -> Dict[str, Any]:
    """Build the frozen narrative -> eligible-token map from live narratives.

    Narratives with no eligible constituents are dropped (recorded under
    ``_meta.dropped``). The result is a point-in-time snapshot intended to be
    committed and read verbatim by the backtest.
    """
    client = client or CMCClient()
    tokens = eligible_tokens if eligible_tokens is not None else load_eligible_tokens()
    by_symbol, by_id = _eligible_lookups(tokens)

    records = client.call("trending_crypto_narratives")
    if not isinstance(records, list):
        raise TypeError(
            f"Expected a list of narrative records, got {type(records).__name__}"
        )

    cat_index = _category_index(client) if expand_with_categories else {}

    narratives: List[Dict[str, Any]] = []
    dropped: List[str] = []
    for record in records:
        name = str(record.get("categoryName", "")).strip()
        slug = record.get("slug")

        members: List[Dict[str, Any]] = []
        category_id = cat_index.get(name.lower()) if expand_with_categories else None
        if category_id:
            members = _category_members(client, category_id)

        token_entries = map_narrative_to_tokens(record, by_symbol, by_id, members)
        if not token_entries:
            dropped.append(name or str(slug))
            continue

        narratives.append(
            {
                "narrative": name,
                "slug": slug,
                "trending_rank": _as_int(record.get("trendingRank")),
                "category_id": category_id,
                "tokens": token_entries,
            }
        )

    return {
        "_meta": {
            "description": "Frozen narrative -> eligible-token map. Point-in-time "
            "snapshot used by the backtest to avoid look-ahead.",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source": "trending_crypto_narratives + CMC categories x eligible_tokens.json",
            "narrative_count": len(narratives),
            "eligible_token_count": len(tokens),
            "expanded_with_categories": expand_with_categories,
            "dropped": dropped,
        },
        "narratives": narratives,
    }


def write_narrative_token_map(
    doc: Dict[str, Any], path: Path = NARRATIVE_MAP_PATH
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n")
    return path


def load_narrative_token_map(path: Path = NARRATIVE_MAP_PATH) -> Dict[str, Any]:
    """Load the frozen narrative -> token map."""
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Run `python scripts/universe.py build-map` first."
        )
    return json.loads(path.read_text())


# ----------------------------------------------------------- verification ----
def verify(
    tokens: List[Dict[str, Any]], narrative_map: Dict[str, Any]
) -> Tuple[bool, List[str]]:
    """Phase 2 acceptance: pairs valid + every narrative has >=1 eligible token."""
    problems: List[str] = []

    if not tokens:
        problems.append("eligible_tokens.json is empty")
    for tok in tokens:
        pair = tok.get("binance_pair") or ""
        if not pair.endswith(BINANCE_QUOTE):
            problems.append(f"{tok.get('symbol')}: invalid Binance pair {pair!r}")

    narratives = narrative_map.get("narratives", [])
    if not narratives:
        problems.append("narrative_token_map.json has no narratives")
    eligible_symbols = {t["symbol"] for t in tokens}
    for nar in narratives:
        ntoks = nar.get("tokens", [])
        if not ntoks:
            problems.append(f"narrative {nar.get('narrative')!r} maps to 0 tokens")
        for t in ntoks:
            if t["symbol"] not in eligible_symbols:
                problems.append(
                    f"narrative {nar.get('narrative')!r} -> {t['symbol']} not eligible"
                )

    return (not problems), problems


def _print_summary(tokens: List[Dict[str, Any]], narrative_map: Dict[str, Any]) -> None:
    print(f"Eligible universe: {len(tokens)} tokens -> {ELIGIBLE_TOKENS_PATH.name}")
    preview = ", ".join(t["symbol"] for t in tokens[:12])
    print(f"  e.g. {preview}{' ...' if len(tokens) > 12 else ''}\n")

    narratives = narrative_map.get("narratives", [])
    dropped = narrative_map.get("_meta", {}).get("dropped", [])
    print(f"Narrative map: {len(narratives)} narratives -> {NARRATIVE_MAP_PATH.name}")
    for nar in narratives:
        syms = [t["symbol"] for t in nar.get("tokens", [])]
        print(
            f"  #{nar.get('trending_rank')} {nar.get('narrative'):<28} "
            f"{len(syms):>2} tokens: {', '.join(syms[:8])}"
            f"{' ...' if len(syms) > 8 else ''}"
        )
    if dropped:
        print(f"  dropped (no eligible tokens): {', '.join(dropped)}")
    print()


def _cmd_build_tokens(client: CMCClient) -> List[Dict[str, Any]]:
    print("Building eligible universe from CMC market-cap ranking x Binance pairs ...")
    tokens = build_eligible_tokens(client)
    write_eligible_tokens(tokens)
    print(f"  wrote {len(tokens)} tokens -> {ELIGIBLE_TOKENS_PATH}")
    return tokens


def _cmd_build_bep20() -> List[Dict[str, Any]]:
    print("Building eligible universe from the official BEP-20 contract list ...")
    tokens = build_eligible_from_bep20()
    priceable = [t for t in tokens if t["priceable"]]
    write_eligible_tokens(
        tokens,
        meta={
            "description": "Official BEP-20 competition universe (symbol + BSC "
            "contract address). Backtest prices each token from its Binance "
            "*USDT spot pair where one exists; tokens flagged priceable=false "
            "are DEX-only on BSC and have no CEX OHLCV history.",
            "source": "reference/bep20_universe.csv x binance exchangeInfo",
            "count": len(tokens),
            "priceable_count": len(priceable),
        },
    )
    print(
        f"  wrote {len(tokens)} tokens ({len(priceable)} priceable via Binance, "
        f"{len(tokens) - len(priceable)} DEX-only) -> {ELIGIBLE_TOKENS_PATH}"
    )
    missing = [t["symbol"] for t in tokens if not t["priceable"]]
    if missing:
        print(f"  DEX-only (no Binance USDT pair): {', '.join(missing)}")
    return tokens


def _cmd_build_map(
    client: CMCClient, tokens: Optional[List[Dict[str, Any]]] = None
) -> Dict[str, Any]:
    print("Building frozen narrative -> token map from live narratives ...")
    doc = build_narrative_token_map(client, eligible_tokens=tokens)
    write_narrative_token_map(doc)
    print(
        f"  wrote {doc['_meta']['narrative_count']} narratives -> {NARRATIVE_MAP_PATH}"
    )
    return doc


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "all"

    if cmd == "build-bep20":
        _cmd_build_bep20()
        return 0

    client = CMCClient()

    if cmd == "build-tokens":
        _cmd_build_tokens(client)
        return 0
    if cmd == "build-map":
        _cmd_build_map(client)
        return 0
    if cmd not in ("all", ""):
        print(
            f"Unknown command {cmd!r}. Use build-bep20 | build-tokens | "
            "build-map | (no arg)."
        )
        return 2

    tokens = _cmd_build_tokens(client)
    doc = _cmd_build_map(client, tokens)
    print()
    _print_summary(tokens, doc)

    ok, problems = verify(tokens, doc)
    if ok:
        print("Phase 2 acceptance: PASS - all pairs valid, every narrative maps to >=1 token.")
        return 0
    print("Phase 2 acceptance: FAIL")
    for p in problems:
        print(f"  - {p}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
