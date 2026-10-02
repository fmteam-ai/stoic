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
    real coin. The same env flag governs every exchange — there is only
    one global kill switch.
    """
    return os.environ.get("BINANCE_LIVE_ENABLED", "false").lower() == "true"


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

    ex._stoic_unsandboxed_testnet = False
    if _is_testnet(account):
        if meta["sandbox"]:
            ex.set_sandbox_mode(True)
            logger.debug("%s instance using SANDBOX", meta["label"])
        else:
            # Kraken / Binance.US have no public sandbox — the instance talks
            # to LIVE endpoints. Reads are allowed; every ORDER write on this
            # instance is refused (see CCXTClient._assert_orders_allowed) so a
            # testnet-flagged account can never place a real order.
            ex._stoic_unsandboxed_testnet = True
            logger.warning(
                "%s has no sandbox; account marked testnet uses live endpoints "
                "for reads only — order placement is BLOCKED.",
                meta["label"],
            )
    return ex


def orders_blocked_reason(account: dict) -> Optional[str]:
    """Why order placement must be refused for this account, or None.

    • testnet (incl. live-disabled) on an exchange with NO sandbox → the
      orders would hit the real exchange.
    • account not flagged testnet while the BINANCE_LIVE_ENABLED master
      switch is off (mirrors the manual route gate in crypto_routes).
    """
    exchange_id = (account.get("exchange_id") or DEFAULT_EXCHANGE_ID).lower()
    meta = EXCHANGES.get(exchange_id)
    if not meta:
        return f"unsupported exchange '{exchange_id}'"
    if not account.get("testnet") and not _live_enabled():
        return "live crypto execution disabled (BINANCE_LIVE_ENABLED=false)"
    if _is_testnet(account) and not meta["sandbox"]:
        return (f"{meta['label']} has no sandbox — testnet/paper account "
                f"cannot place orders (would execute on the live exchange)")
    return None


# ────────────────────────────── Client ──────────────────────────────

class CCXTClient:
    """One-shot async client. Use via `async with` to guarantee close()."""

    def __init__(self, account: dict):
        self.account = account
        self.exchange = None

    async def __aenter__(self) -> "CCXTClient":
        self.exchange = _new_exchange(self.account)
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

    async def fetch_order(self, order_id: str, symbol: str,
                          params: Optional[dict] = None) -> dict:
        # `params` carries venue flags such as {"trigger": True} for OKX /
        # KuCoin conditional (algo / stop) orders.
        return await self.exchange.fetch_order(order_id, symbol, dict(params or {}))

    async def fetch_order_by_client_id(self, client_order_id: str, symbol: str,
                                       params: Optional[dict] = None) -> dict:
        """Look an order up by OUR deterministic clientOrderId — used to
        resolve a submission whose response was lost (post-dispatch
        uncertainty). ccxt maps the unified `clientOrderId` param to
        origClientOrderId (Binance) / clOrdId|algoClOrdId (OKX) /
        clientOid (KuCoin)."""
        p = {"clientOrderId": client_order_id}
        p.update(params or {})
        return await self.exchange.fetch_order(None, symbol, p)

    async def fetch_open_orders(self, symbol: str) -> list:
        return await self.exchange.fetch_open_orders(symbol)

    # ------- market metadata (best effort; never raises) -------
    async def load_markets(self) -> None:
        try:
            await self.exchange.load_markets()
        except Exception as e:  # noqa: BLE001 — precision is best-effort
            logger.warning("load_markets failed: %s", e)

    def _market(self, symbol: str) -> Optional[dict]:
        try:
            m = self.exchange.market(symbol)
            return m if isinstance(m, dict) else None
        except Exception:  # noqa: BLE001
            return None

    def is_contract(self, symbol: str) -> bool:
        m = self._market(symbol)
        return bool(m and m.get("contract") is True)

    def amount_to_precision(self, symbol: str, amount: float) -> float:
        """Exchange lot-step rounding (ccxt TRUNCATEs, never rounds up —
        a protective sell can never exceed the held balance)."""
        try:
            return float(self.exchange.amount_to_precision(symbol, amount))
        except Exception:  # noqa: BLE001 — markets not loaded / mock
            return float(amount)

    def price_to_precision(self, symbol: str, price: float) -> float:
        try:
            return float(self.exchange.price_to_precision(symbol, price))
        except Exception:  # noqa: BLE001
            return float(price)

    # ------- write -------
    def _assert_orders_allowed(self) -> None:
        if getattr(self.exchange, "_stoic_unsandboxed_testnet", False):
            raise RuntimeError(
                "order refused: testnet account on an exchange without a "
                "sandbox would execute on the LIVE exchange")

    @staticmethod
    def _order_params(client_order_id: Optional[str]) -> dict:
        # ccxt unified `clientOrderId` — deterministic per intent so a retry
        # of the same order is rejected as a duplicate by the exchange.
        return {"clientOrderId": client_order_id} if client_order_id else {}

    async def create_market_order(self, symbol: str, side: str, amount: float,
                                  client_order_id: Optional[str] = None) -> dict:
        self._assert_orders_allowed()
        return await self.exchange.create_order(
            symbol, "market", side.lower(), amount, None,
            self._order_params(client_order_id))

    async def create_limit_order(self, symbol: str, side: str, amount: float, price: float,
                                 client_order_id: Optional[str] = None) -> dict:
        self._assert_orders_allowed()
        return await self.exchange.create_order(
            symbol, "limit", side.lower(), amount, price,
            self._order_params(client_order_id))

    async def cancel_order(self, order_id: str, symbol: str,
                           params: Optional[dict] = None) -> dict:
        return await self.exchange.cancel_order(order_id, symbol, dict(params or {}))

    # ------- protective orders (see crypto_bridge/protective.py) -------
    async def create_stop_loss_order(self, symbol: str, side: str, amount: float,
                                     stop_price: float,
                                     client_order_id: Optional[str] = None,
                                     reduce_only: bool = False) -> dict:
        """Exchange-side STOP-MARKET exit via the ccxt unified
        `stopLossPrice` param (Binance spot → STOP_LOSS, Binance perps →
        STOP_MARKET, OKX → conditional algo, KuCoin → stop=loss, Kraken →
        stop-loss)."""
        self._assert_orders_allowed()
        params = self._order_params(client_order_id)
        params["stopLossPrice"] = stop_price
        if reduce_only:
            params["reduceOnly"] = True
        return await self.exchange.create_order(
            symbol, "market", side.lower(), amount, None, params)

    async def create_take_profit_limit_order(self, symbol: str, side: str,
                                             amount: float, price: float,
                                             client_order_id: Optional[str] = None,
                                             reduce_only: bool = False) -> dict:
        """Resting LIMIT exit at the take-profit price."""
        self._assert_orders_allowed()
        params = self._order_params(client_order_id)
        if reduce_only:
            params["reduceOnly"] = True
        return await self.exchange.create_order(
            symbol, "limit", side.lower(), amount, price, params)

    async def create_binance_spot_oco(self, symbol: str, side: str, amount: float,
                                      stop_price: float, take_profit: float, *,
                                      list_client_order_id: str,
                                      sl_client_order_id: str,
                                      tp_client_order_id: str) -> dict:
        """Binance spot native OCO (POST /api/v3/orderList/oco — ccxt has no
        unified OCO, so this uses the implicit endpoint). One leg filling
        makes the exchange expire the other.

        SELL (protects a long):  above = LIMIT_MAKER @ TP, below = STOP_LOSS @ SL
        BUY  (protects a short): above = STOP_LOSS @ SL,  below = TAKE_PROFIT @ TP

        Returns {"list_id", "sl": {id, clientOrderId}, "tp": {...}, "raw"}."""
        self._assert_orders_allowed()
        ex = self.exchange
        market_id = symbol.replace("/", "")
        try:
            market_id = ex.market_id(symbol)
        except Exception:  # noqa: BLE001 — markets not loaded / mock
            pass
        def _s(fn, v):
            try:
                out = fn(symbol, v)
                if isinstance(out, str):
                    return out
            except Exception:  # noqa: BLE001 — markets not loaded / mock
                pass
            return repr(float(v))
        sp = _s(ex.price_to_precision, stop_price)
        tp = _s(ex.price_to_precision, take_profit)
        req = {
            "symbol": market_id,
            "side": side.upper(),
            "quantity": _s(ex.amount_to_precision, amount),
            "listClientOrderId": list_client_order_id,
            "newOrderRespType": "FULL",
        }
        if side.lower() == "sell":
            req.update({"aboveType": "LIMIT_MAKER", "abovePrice": tp,
                        "aboveClientOrderId": tp_client_order_id,
                        "belowType": "STOP_LOSS", "belowStopPrice": sp,
                        "belowClientOrderId": sl_client_order_id})
        else:
            req.update({"aboveType": "STOP_LOSS", "aboveStopPrice": sp,
                        "aboveClientOrderId": sl_client_order_id,
                        "belowType": "TAKE_PROFIT", "belowStopPrice": tp,
                        "belowClientOrderId": tp_client_order_id})
        raw = await ex.private_post_orderlist_oco(req)
        legs = {}
        for rep in (raw or {}).get("orderReports") or (raw or {}).get("orders") or []:
            cid = str(rep.get("clientOrderId") or "")
            leg = {"id": str(rep.get("orderId") or ""), "clientOrderId": cid}
            if cid == sl_client_order_id:
                legs["sl"] = leg
            elif cid == tp_client_order_id:
                legs["tp"] = leg
        if "sl" not in legs:
            raise RuntimeError(f"OCO response missing stop-loss leg: {str(raw)[:200]}")
        return {"list_id": str((raw or {}).get("orderListId") or ""),
                "sl": legs["sl"], "tp": legs.get("tp"), "raw": raw}

    async def create_okx_algo_oco(self, symbol: str, side: str, amount: float,
                                  stop_price: float, take_profit: float,
                                  client_order_id: Optional[str] = None) -> dict:
        """OKX native algo OCO: ccxt turns stopLossPrice + takeProfitPrice on
        one createOrder into ordType='oco' (privatePostTradeOrderAlgo)."""
        self._assert_orders_allowed()
        params = self._order_params(client_order_id)
        params.update({"stopLossPrice": stop_price, "takeProfitPrice": take_profit})
        return await self.exchange.create_order(
            symbol, "market", side.lower(), amount, None, params)

    async def close_position_market(self, symbol: str, side: str, amount: float,
                                    client_order_id: Optional[str] = None,
                                    reduce_only: bool = False) -> dict:
        """Emergency flatten — market order on the EXIT side."""
        self._assert_orders_allowed()
        params = self._order_params(client_order_id)
        if reduce_only:
            params["reduceOnly"] = True
        return await self.exchange.create_order(
            symbol, "market", side.lower(), amount, None, params)


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
