"""Generic CCXT spot client — supports multiple exchanges through one
async interface.

Replaces the prior Binance-only wrapper while remaining wire-compatible
(BinanceClient is re-exported as an alias for backwards compat).

Supported exchanges (`exchange_id` values):
  • binance     · Binance global Spot   (testnet at binance.vision)
  • binanceus   · Binance.US Spot       (no testnet — defaults to live)
  • kraken      · Kraken Spot           (no testnet — defaults to live)
  • okx         · OKX Spot              (testnet via x-simulated-trading header)
  • kucoin      · KuCoin Spot           (sandbox supported)

Multi-user safety:
  • Each call instantiates a fresh ccxt exchange — no global key reuse.
  • Decryption happens here; plaintext never leaves this module.
  • Rate-limiting is delegated to ccxt's built-in throttler.
"""
from __future__ import annotations
import os
import logging
from typing import Optional

import ccxt.async_support as ccxt  # type: ignore[import]

from secrets_vault import decrypt as vault_decrypt

logger = logging.getLogger("crypto.ccxt")


# ─────────────────────────── Exchange registry ───────────────────────────

# Per-exchange metadata: which ccxt class, whether a sandbox/testnet exists,
# whether a passphrase is required (OKX, KuCoin), and how internal STOIC
# symbols map to the exchange's preferred quote currency. Kraken quotes
# BTC against fiat USD natively (BTC/USD), every other exchange uses USDT.
# `ping_url` is hit by the reachability probe — a public, unauthenticated
# endpoint that returns quickly on success.
EXCHANGES = {
    "binance":   {"klass": "binance",   "sandbox": True,  "passphrase": False, "default_quote": "USDT", "label": "Binance Global",
                  "ping_url": "https://api.binance.com/api/v3/ping"},
    "binanceus": {"klass": "binanceus", "sandbox": False, "passphrase": False, "default_quote": "USDT", "label": "Binance.US",
                  "ping_url": "https://api.binance.us/api/v3/ping"},
    "kraken":    {"klass": "kraken",    "sandbox": False, "passphrase": False, "default_quote": "USD",  "label": "Kraken",
                  "ping_url": "https://api.kraken.com/0/public/Time"},
    "okx":       {"klass": "okx",       "sandbox": True,  "passphrase": True,  "default_quote": "USDT", "label": "OKX",
                  "ping_url": "https://www.okx.com/api/v5/public/time"},
    "kucoin":    {"klass": "kucoin",    "sandbox": True,  "passphrase": True,  "default_quote": "USDT", "label": "KuCoin",
                  "ping_url": "https://api.kucoin.com/api/v1/timestamp"},
}
SUPPORTED_EXCHANGES = list(EXCHANGES.keys())
DEFAULT_EXCHANGE_ID = "binance"


# ─────────────────────── Reachability probe ───────────────────────
# Cluster outbound IP is fixed (Iowa/US right now), so reachability is
# stable across requests. Cache the probe results to avoid hammering
# exchange ping endpoints. TTL = 5 minutes.
_REACHABILITY_CACHE: dict = {"checked_at": 0.0, "results": {}}
_REACHABILITY_TTL_SEC = 300  # 5 min


async def check_reachability(force: bool = False) -> dict:
    """Probe each supported exchange's public ping endpoint with a tight
    timeout, returning ``{exchange_id: {reachable: bool, status_code, error}}``.

    Results are cached for 5 minutes — the cluster IP rarely changes, so
    re-probing every page load is wasteful and could trip rate limits.
    """
    import time
    import asyncio
    import httpx

    now = time.time()
    cache = _REACHABILITY_CACHE
    if not force and cache["results"] and (now - cache["checked_at"]) < _REACHABILITY_TTL_SEC:
        return cache["results"]

    async def _probe(eid: str, url: str) -> tuple[str, dict]:
        try:
            async with httpx.AsyncClient(timeout=6.0, follow_redirects=True) as c:
                r = await c.get(url)
            # Binance global returns HTTP 451 when geo-blocked; treat any 4xx
            # except 401/403 (which mean reachable but auth-required) as
            # unreachable for the purpose of "can the cluster talk to this API".
            ok = r.status_code == 200
            return eid, {
                "reachable": ok,
                "status_code": r.status_code,
                "error": None if ok else (
                    "Geo-blocked (451)" if r.status_code == 451
                    else f"HTTP {r.status_code}"
                ),
            }
        except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as e:
            return eid, {"reachable": False, "status_code": None, "error": f"Network: {type(e).__name__}"}
        except Exception as e:  # noqa: BLE001
            return eid, {"reachable": False, "status_code": None, "error": str(e)[:120]}

    pairs = await asyncio.gather(*[
        _probe(eid, meta["ping_url"])
        for eid, meta in EXCHANGES.items() if meta.get("ping_url")
    ])
    results = dict(pairs)
    cache["checked_at"] = now
    cache["results"] = results
    return results


def _exchange_meta(exchange_id: str) -> dict:
    meta = EXCHANGES.get((exchange_id or "").lower())
    if not meta:
        raise RuntimeError(
            f"Unsupported exchange '{exchange_id}'. "
            f"Choose one of: {', '.join(SUPPORTED_EXCHANGES)}"
        )
    return meta


def _live_enabled() -> bool:
    """Master env switch for live (non-testnet) crypto execution.

    Defaults to FALSE so a misconfigured account can't accidentally trade
    real coin. A13-1 (P0-01 step 1): BOTH switches must be on — the operator
    kill switch CRYPTO_LIVE_TRADING_ENABLED (default off) AND the exchange flag
    BINANCE_LIVE_ENABLED. Engine, routes and the Crypto UI all read this one function.
    """
    return (os.environ.get("CRYPTO_LIVE_TRADING_ENABLED", "false").lower() == "true"
            and os.environ.get("BINANCE_LIVE_ENABLED", "false").lower() == "true")


def _wants_live(account: dict) -> bool:
    """Operator INTENT for real-money execution (ignores the kill switch — used to refuse, never downgrade)."""
    if account.get("testnet"):
        return False
    return bool(account.get("live")) or account.get("mode") == "live"


def _is_testnet(account: dict) -> bool:
    """Decide whether to use sandbox/testnet for this account.

    Defence-in-depth — any one of these wins → testnet:
      1. account['testnet'] truthy → testnet
      2. account['live'] falsy → testnet
      3. global BINANCE_LIVE_ENABLED=false → testnet (master kill switch)
    """
    if account.get("testnet"):
        return True
    if not account.get("live"):
        return True
    if not _live_enabled():
        return True
    return False


def _decrypt_creds(account: dict) -> tuple[str, str, Optional[str]]:
    """Pull and decrypt API key + secret (+ optional passphrase) from an account doc."""
    creds = account.get("creds") or {}
    enc_key = creds.get("api_key")
    enc_sec = creds.get("api_secret")
    enc_pass = creds.get("api_passphrase")  # OKX/KuCoin only
    if not enc_key or not enc_sec:
        raise RuntimeError("Crypto account is missing api_key/api_secret")
    return (
        vault_decrypt(enc_key),
        vault_decrypt(enc_sec),
        vault_decrypt(enc_pass) if enc_pass else None,
    )


def _new_exchange(account: dict):
    """Build a fresh, configured ccxt exchange instance for one operation.

    Dispatches by `account['exchange_id']` (defaults to 'binance' for legacy
    accounts written before multi-exchange support landed).
    """
    exchange_id = (account.get("exchange_id") or DEFAULT_EXCHANGE_ID).lower()
    meta = _exchange_meta(exchange_id)
    if _is_testnet(account) and not meta["sandbox"]:
        # N97-6 — Kraken / Binance.US have no sandbox: a "testnet" account would hit the
        # REAL exchange with real money while every live gate sees it as testnet. Refuse.
        raise SandboxUnavailable(f"{meta['label']} has no sandbox/testnet — a testnet account cannot be "
                                 "used here; connect it as LIVE (and arm both live switches) or remove it.")
    api_key, api_secret, api_pass = _decrypt_creds(account)

    config: dict = {
        "apiKey": api_key,
        "secret": api_secret,
        "enableRateLimit": True,
        "options": {"defaultType": "spot"},
    }
    if meta["passphrase"]:
        if not api_pass:
            raise RuntimeError(
                f"{meta['label']} requires an API passphrase — add it to the account."
            )
        config["password"] = api_pass

    klass = getattr(ccxt, meta["klass"])
    ex = klass(config)

    if _is_testnet(account):
        ex.set_sandbox_mode(True)
        logger.debug("%s instance using SANDBOX", meta["label"])
    return ex


class SandboxUnavailable(RuntimeError):
    """N97-6 — testnet requested on an exchange without a sandbox (would trade real money)."""


def sandbox_available(account: dict) -> bool:
    return bool(EXCHANGES.get((account.get("exchange_id") or DEFAULT_EXCHANGE_ID).lower(), {}).get("sandbox"))


# ────────────────────────────── Client ──────────────────────────────

class CCXTClient:
    """One-shot async client. Use via `async with` to guarantee close()."""

    def __init__(self, account: dict):
        self.account = account
        self.exchange = None

    async def __aenter__(self) -> "CCXTClient":
        self.exchange = _new_exchange(self.account)
        # N98-8 — market() / amount_to_precision() need the market table on a FRESH client
        await self.exchange.load_markets()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self.exchange is not None:
            try:
                await self.exchange.close()
            except Exception:  # noqa: BLE001
                pass
            self.exchange = None

    # ------- read-only -------
    async def fetch_balance(self) -> dict:
        return await self.exchange.fetch_balance()

    async def fetch_ticker(self, symbol: str) -> dict:
        return await self.exchange.fetch_ticker(symbol)

    async def fetch_order(self, order_id: str, symbol: str) -> dict:
        return await self.exchange.fetch_order(order_id, symbol)

    async def fetch_open_orders(self, symbol: str) -> list:
        return await self.exchange.fetch_open_orders(symbol)

    # ------- write -------
    async def create_market_order(self, symbol: str, side: str, amount: float,
                                  client_order_id: str | None = None) -> dict:
        params = {"clientOrderId": client_order_id} if client_order_id else {}
        return await self.exchange.create_order(symbol, "market", side.lower(), amount, None, params)

    async def create_limit_order(self, symbol: str, side: str, amount: float, price: float,
                                 client_order_id: str | None = None) -> dict:
        params = {"clientOrderId": client_order_id} if client_order_id else {}
        return await self.exchange.create_order(symbol, "limit", side.lower(), amount, price, params)

    async def cancel_order(self, order_id: str, symbol: str) -> dict:
        return await self.exchange.cancel_order(order_id, symbol)

    def amount_to_precision(self, symbol: str, amount: float) -> float:
        return float(self.exchange.amount_to_precision(symbol, amount))

    # ------- A13 P0-01: truth + protection -------
    async def fetch_order_by_client_id(self, symbol: str, client_order_id: str) -> dict | None:
        """Broker truth for a deterministic client order id (None = never reached the exchange)."""
        try:
            return await self.exchange.fetch_order(None, symbol, {"clientOrderId": client_order_id})
        except Exception as e:  # noqa: BLE001
            if "OrderNotFound" in type(e).__name__ or "does not exist" in str(e).lower():
                return None
            raise

    async def place_oco_protection(self, symbol: str, position_side: str, amount: float,
                                   stop_loss: float, take_profit: float, client_order_id: str) -> dict:
        """Exchange-side SL/TP as ONE OCO list on the opposite side (Binance spot).
        Other venues have no uniform OCO → raise so the caller fails closed."""
        eid = (self.account.get("exchange_id") or DEFAULT_EXCHANGE_ID).lower()
        if eid != "binance" or not hasattr(self.exchange, "privatePostOrderlistOco"):
            raise RuntimeError(f"{eid}: exchange-side OCO protection unsupported")
        market = self.exchange.market(symbol)
        close_side = "SELL" if position_side.lower() == "buy" else "BUY"
        stop_limit = stop_loss * (0.998 if close_side == "SELL" else 1.002)
        # N98-10 — current endpoint POST /api/v3/orderList/oco (the /order/oco form is deprecated).
        # For a SELL list: above = take-profit LIMIT_MAKER, below = STOP_LOSS_LIMIT; mirrored for BUY.
        tp_leg = {"Type": "LIMIT_MAKER", "Price": self.exchange.price_to_precision(symbol, take_profit),
                  "ClientOrderId": f"{client_order_id}-tp"}
        sl_leg = {"Type": "STOP_LOSS_LIMIT", "StopPrice": self.exchange.price_to_precision(symbol, stop_loss),
                  "Price": self.exchange.price_to_precision(symbol, stop_limit), "TimeInForce": "GTC",
                  "ClientOrderId": f"{client_order_id}-sl"}
        above, below = (tp_leg, sl_leg) if close_side == "SELL" else (sl_leg, tp_leg)
        params = {"symbol": market["id"], "side": close_side,
                  "quantity": self.exchange.amount_to_precision(symbol, amount),
                  "listClientOrderId": f"{client_order_id}-oco"}
        params.update({f"above{k}": v for k, v in above.items()})
        params.update({f"below{k}": v for k, v in below.items()})
        resp = await self.exchange.privatePostOrderlistOco(params)
        return {"list_id": str(resp.get("orderListId") or ""), "list_client_order_id": params["listClientOrderId"],
                "tp_client_order_id": f"{client_order_id}-tp", "sl_client_order_id": f"{client_order_id}-sl",
                "raw_status": resp.get("listOrderStatus")}

    async def fetch_oco_status(self, list_client_order_id: str) -> dict | None:
        """None = the list does not exist at the exchange; raises on transport errors (uncertain)."""
        if not hasattr(self.exchange, "privateGetOrderlist"):
            return None
        try:
            return await self.exchange.privateGetOrderlist({"origClientOrderId": list_client_order_id})
        except Exception as e:  # noqa: BLE001
            if "does not exist" in str(e).lower() or "-2018" in str(e) or "OrderNotFound" in type(e).__name__:
                return None
            raise


# Backwards-compat alias — old code calls BinanceClient(account).
BinanceClient = CCXTClient


# ─────────────────── Lightweight helpers (no live call) ───────────────────

def normalize_symbol(stoic_symbol: str, exchange_id: str = DEFAULT_EXCHANGE_ID) -> str:
    """Map our internal symbol to ccxt's `BASE/QUOTE` notation for `exchange_id`.

    Kraken quotes BTC against fiat USD natively (BTC/USD).
    Binance / Binance.US / OKX / KuCoin use BTC/USDT.

    BTCUSD  → BTC/USDT (or BTC/USD on Kraken)
    ETHUSD  → ETH/USDT (or ETH/USD on Kraken)
    Already-slashed pairs are returned as-is.
    """
    if not stoic_symbol:
        return ""
    s = stoic_symbol.upper().replace(" ", "")
    if "/" in s:
        return s
    quote = _exchange_meta(exchange_id).get("default_quote", "USDT")
    table = {
        "BTCUSD": f"BTC/{quote}",
        "ETHUSD": f"ETH/{quote}",
        "SOLUSD": f"SOL/{quote}",
        "BTCUSDT": "BTC/USDT",
        "ETHUSDT": "ETH/USDT",
    }
    return table.get(s, s)


def is_crypto_symbol(symbol: str) -> bool:
    """True iff this symbol is a crypto pair we can route to a CCXT exchange."""
    if not symbol:
        return False
    n = normalize_symbol(symbol)
    return "/" in n and any(n.startswith(b + "/") for b in ("BTC", "ETH", "SOL"))
