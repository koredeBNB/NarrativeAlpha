"""CMC Agent Hub client for NarrativeAlpha.

Provides a thin MCP (JSON-RPC 2.0) client for the CoinMarketCap MCP server with a
REST fallback, plus helpers to turn CMC's display-formatted payloads into clean
Python values.

CMC quirks handled here (see PRD.md sec.5):
  - MCP tool results wrap a JSON string inside ``content[].text``.
  - Tabular payloads arrive as ``{"headers": [...], "rows": [...]}`` with nested
    sub-tables (e.g. ``topCoinList``).
  - Numeric values are display-formatted strings such as ``"1.41 T"`` or
    ``"+2.39%"`` and must be normalized to floats.

Run ``python scripts/cmc_client.py`` for a self-test that exercises the
``normalize`` helper and a live ``trending_crypto_narratives`` call.
"""

from __future__ import annotations

import json
import os
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

import requests

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dotenv is optional at runtime
    def load_dotenv(*_args, **_kwargs):  # type: ignore
        return False


# Magnitude suffixes used in CMC display strings -> base-10 exponent.
_MAGNITUDE = {"K": 3, "M": 6, "B": 9, "T": 12}
_MAGNITUDE_RE = re.compile(r"^([+-]?\d*\.?\d+)\s*([KkMmBbTt])$")


class CMCError(RuntimeError):
    """Raised when the CMC MCP/REST API returns an error or malformed payload."""


def normalize(value: Any) -> Any:
    """Convert a CMC display value to a clean Python value.

    Examples
    --------
    >>> normalize("1.41 T")
    1410000000000.0
    >>> normalize("+2.39%")
    0.0239
    >>> normalize("33.93 B")
    33930000000.0
    >>> normalize("Solana")
    'Solana'

    Percentages are returned as fractions (``"+2.39%" -> 0.0239``). Magnitude
    suffixes (K/M/B/T) are expanded. Non-numeric strings, lists, dicts and
    ``None`` are returned unchanged. Decimal arithmetic is used so results round
    to the same float as the equivalent Python literal.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return value

    text = value.strip()
    if text == "":
        return None

    cleaned = text.replace("$", "").replace(",", "").strip()

    # Percentages -> fraction.
    if cleaned.endswith("%"):
        body = cleaned[:-1].strip()
        try:
            return float(Decimal(body) / Decimal(100))
        except InvalidOperation:
            return value

    # Magnitude suffix (e.g. "1.41 T").
    match = _MAGNITUDE_RE.match(cleaned)
    if match:
        mantissa, suffix = match.group(1), match.group(2).upper()
        try:
            return float(Decimal(mantissa) * (Decimal(10) ** _MAGNITUDE[suffix]))
        except InvalidOperation:
            return value

    # Plain number.
    try:
        return float(Decimal(cleaned))
    except InvalidOperation:
        return value


def _is_table(obj: Any) -> bool:
    return isinstance(obj, dict) and "headers" in obj and "rows" in obj


def _find_table(obj: Any) -> Optional[Dict[str, Any]]:
    """Recursively locate the first ``{headers, rows}`` table in a payload."""
    if _is_table(obj):
        return obj
    if isinstance(obj, dict):
        for sub in obj.values():
            found = _find_table(sub)
            if found is not None:
                return found
    return None


def parse_table(table: Dict[str, Any], normalize_values: bool = True) -> List[Dict[str, Any]]:
    """Turn a ``{headers, rows}`` table into a list of dicts.

    Nested sub-tables (such as ``topCoinList``) are expanded recursively. When
    ``normalize_values`` is true, scalar cell values are passed through
    :func:`normalize`.
    """
    headers = table.get("headers", [])
    records: List[Dict[str, Any]] = []
    for row in table.get("rows", []):
        record: Dict[str, Any] = {}
        for key, cell in zip(headers, row):
            if _is_table(cell):
                record[key] = parse_table(cell, normalize_values)
            elif normalize_values:
                record[key] = normalize(cell)
            else:
                record[key] = cell
        records.append(record)
    return records


class CMCClient:
    """Minimal MCP JSON-RPC client for the CMC Agent Hub with a REST fallback."""

    MCP_URL = "https://mcp.coinmarketcap.com/mcp"
    REST_BASE = "https://pro-api.coinmarketcap.com"
    PROTOCOL_VERSION = "2025-03-26"
    CLIENT_INFO = {"name": "narrativealpha", "version": "0.1.0"}

    # Tools that have a usable REST equivalent for fallback. (The core
    # ``trending_crypto_narratives`` signal is MCP-exclusive and has no REST path.)
    REST_TOOL_MAP = {
        "get_global_metrics_latest": "/v1/global-metrics/quotes/latest",
        "search_cryptos": "/v1/cryptocurrency/map",
        "get_crypto_categories": "/v1/cryptocurrency/categories",
    }

    def __init__(
        self,
        api_key: Optional[str] = None,
        mcp_url: Optional[str] = None,
        timeout: int = 30,
    ) -> None:
        load_dotenv()
        self.api_key = api_key or os.getenv("CMC_API_KEY")
        if not self.api_key:
            raise CMCError(
                "CMC_API_KEY not set. Copy .env.example to .env and add your key."
            )
        self.mcp_url = mcp_url or self.MCP_URL
        self.timeout = timeout
        self.session = requests.Session()
        self._rpc_id = 0
        self._initialized = False
        self._mcp_session_id: Optional[str] = None

    # ------------------------------------------------------------------ MCP --
    def _next_id(self) -> int:
        self._rpc_id += 1
        return self._rpc_id

    def _mcp_headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "X-CMC-MCP-API-KEY": self.api_key,
        }
        if self._mcp_session_id:
            headers["Mcp-Session-Id"] = self._mcp_session_id
        return headers

    @staticmethod
    def _parse_jsonrpc(resp: requests.Response) -> Any:
        """Decode a JSON-RPC reply from either JSON or an SSE stream."""
        content_type = resp.headers.get("Content-Type", "")
        body: Any = None
        if "text/event-stream" in content_type:
            for line in resp.text.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    chunk = line[len("data:"):].strip()
                    if chunk and chunk != "[DONE]":
                        body = json.loads(chunk)
        else:
            body = resp.json()
        if body is None:
            raise CMCError("Empty MCP response")
        if body.get("error"):
            raise CMCError(f"MCP error: {body['error']}")
        return body.get("result")

    def _post_mcp(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        is_notification: bool = False,
    ) -> Any:
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if not is_notification:
            payload["id"] = self._next_id()
        if params is not None:
            payload["params"] = params

        resp = self.session.post(
            self.mcp_url, json=payload, headers=self._mcp_headers(), timeout=self.timeout
        )
        session_id = resp.headers.get("Mcp-Session-Id")
        if session_id:
            self._mcp_session_id = session_id

        if is_notification:
            return None

        resp.raise_for_status()
        return self._parse_jsonrpc(resp)

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        self._post_mcp(
            "initialize",
            {
                "protocolVersion": self.PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": self.CLIENT_INFO,
            },
        )
        self._post_mcp("notifications/initialized", is_notification=True)
        self._initialized = True

    @staticmethod
    def _unwrap_content(result: Dict[str, Any]) -> Any:
        """Extract and JSON-decode the text payload from an MCP tool result."""
        content = result.get("content")
        if not content:
            return result
        texts = [
            item.get("text")
            for item in content
            if item.get("type") == "text" and item.get("text") is not None
        ]
        if not texts:
            return result
        joined = "\n".join(texts)
        try:
            return json.loads(joined)
        except (json.JSONDecodeError, TypeError):
            return joined

    def _call_mcp(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        self._ensure_initialized()
        result = self._post_mcp(
            "tools/call", {"name": tool_name, "arguments": arguments}
        )
        if not isinstance(result, dict):
            raise CMCError(f"Unexpected MCP result for {tool_name!r}: {result!r}")
        if result.get("isError"):
            raise CMCError(f"Tool {tool_name!r} returned error: {result.get('content')}")
        return self._unwrap_content(result)

    # ----------------------------------------------------------------- REST --
    def rest(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """Call a CMC Pro REST endpoint (used as a fallback)."""
        url = self.REST_BASE + path
        headers = {"X-CMC_PRO_API_KEY": self.api_key, "Accept": "application/json"}
        resp = self.session.get(
            url, headers=headers, params=params or {}, timeout=self.timeout
        )
        resp.raise_for_status()
        return resp.json()

    def _rest_fallback(self, tool_name: str, arguments: Dict[str, Any]) -> Optional[Any]:
        path = self.REST_TOOL_MAP.get(tool_name)
        if not path:
            return None
        return self.rest(path, params=arguments)

    # ----------------------------------------------------------------- API ---
    def call(
        self,
        tool_name: str,
        arguments: Optional[Dict[str, Any]] = None,
        parse: bool = True,
    ) -> Any:
        """Call a CMC tool and return a clean Python result.

        Tries the MCP server first; on a network/protocol error it falls back to
        a REST endpoint when one is mapped. When ``parse`` is true and the
        payload contains a ``{headers, rows}`` table, the table is returned as a
        list of normalized row dicts.
        """
        arguments = arguments or {}
        try:
            payload = self._call_mcp(tool_name, arguments)
        except (requests.RequestException, CMCError):
            fallback = self._rest_fallback(tool_name, arguments)
            if fallback is None:
                raise
            payload = fallback

        if not parse:
            return payload

        table = _find_table(payload)
        if table is not None:
            return parse_table(table)
        return payload

    def get_narratives(self) -> List[Dict[str, Any]]:
        """Convenience wrapper for the core ``trending_crypto_narratives`` signal."""
        return self.call("trending_crypto_narratives")


def _self_test() -> int:
    """Verify Phase 0 acceptance: normalize() and a live narratives call."""
    assert normalize("1.41 T") == 1.41e12, normalize("1.41 T")
    assert normalize("+2.39%") == 0.0239, normalize("+2.39%")
    assert normalize("33.93 B") == 33.93e9, normalize("33.93 B")
    assert normalize("-17.22%") == -0.1722, normalize("-17.22%")
    assert normalize("Solana") == "Solana"
    assert normalize(None) is None
    print("normalize() checks passed.")

    client = CMCClient()
    rows = client.call("trending_crypto_narratives")
    if not isinstance(rows, list) or not rows:
        raise CMCError("trending_crypto_narratives returned no rows")
    print(f"trending_crypto_narratives returned {len(rows)} parsed rows.")

    for row in rows[:3]:
        name = row.get("categoryName", "?")
        perf7d = row.get("volumeWeightedPricePerfVsCryptoMarketCap7d")
        coins = [c.get("coinSymbol") for c in (row.get("topCoinList") or [])]
        print(f"  - {str(name).strip():<28} vw_perf_vs_mkt_7d={perf7d}  top={coins}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
