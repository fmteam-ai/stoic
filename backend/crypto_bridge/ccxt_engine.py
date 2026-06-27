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
EXCHANGES = {
    "binance":   {"klass": "binance",   "sandbox": True,  "passphrase": False, "default_quote": "USDT", "label": "Binance Global"},
    "binanceus": {"klass": "binanceus", "sandbox": False, "passphrase": False, "default_quote": "USDT", "label": "Binance.US"},
    "kraken":    {"klass": "kraken",    "sandbox": False, "passphrase": False, "default_quote": "USD",  "label": "Kraken"},
    "okx":       {"klass": "okx",       "sandbox": True,  "passphrase": True,  "default_quote": "USDT", "label": "OKX"},
    "kucoin":    {"klass": "kucoin",    "sandbox": True,  "passphrase": True,  "default_quote": "USDT", "label": "KuCoin"},
}
SUPPORTED_EXCHANGES = list(EXCHANGES.keys())
DEFAULT_EXCHANGE_ID = "binance"


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

    if _is_testnet(account):
        if meta["sandbox"]:
            ex.set_sandbox_mode(True)
            logger.debug("%s instance using SANDBOX", meta["label"])
        else:
            # Kraken / Binance.US have no public sandbox — log + proceed live
            # (any orders are still blocked by the global BINANCE_LIVE_ENABLED
            # gate at the route layer, so this is a no-op for execution).
            logger.warning(
                "%s has no sandbox; account marked testnet will use live "
                "endpoints but route-layer gates still block real orders.",
                meta["label"],
            )
    return ex


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

    async def fetch_order(self, order_id: str, symbol: str) -> dict:
        return await self.exchange.fetch_order(order_id, symbol)

    async def fetch_open_orders(self, symbol: str) -> list:
        return await self.exchange.fetch_open_orders(symbol)

    # ------- write -------
    async def create_market_order(self, symbol: str, side: str, amount: float) -> dict:
        return await self.exchange.create_order(symbol, "market", side.lower(), amount)

    async def create_limit_order(self, symbol: str, side: str, amount: float, price: float) -> dict:
        return await self.exchange.create_order(symbol, "limit", side.lower(), amount, price)

    async def cancel_order(self, order_id: str, symbol: str) -> dict:
        return await self.exchange.cancel_order(order_id, symbol)


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
