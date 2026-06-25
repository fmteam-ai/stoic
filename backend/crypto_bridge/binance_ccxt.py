"""Binance Spot client — thin async wrapper around `ccxt.async_support`.

Responsibilities (and only these):
  • Build a per-call exchange instance with the user's encrypted API keys
  • Honour testnet-first via `set_sandbox_mode(True)` unless the account
    explicitly flips `live=True` (which the UI/route layer must validate)
  • Provide the small set of operations the BinanceCCXTEngine needs:
      fetch_balance, fetch_ticker, create_market_order, create_limit_order,
      fetch_order, cancel_order
  • Always `await exchange.close()` to avoid aiohttp unclosed-session warnings

Multi-user safety:
  • Each call instantiates a fresh exchange — no global key reuse.
  • Decryption happens here and the plaintext never leaves this module.
  • Rate-limiting is delegated to ccxt's built-in throttler
    (`enableRateLimit=True`).

This wrapper is intentionally exchange-agnostic at the method level so we
can add OKX / Bybit later by swapping the constructor.
"""
from __future__ import annotations
import os
import logging
from typing import Optional

import ccxt.async_support as ccxt  # type: ignore[import]

from secrets_vault import decrypt as vault_decrypt

logger = logging.getLogger("crypto.binance")


def _live_enabled() -> bool:
    """Master env switch for live (non-testnet) crypto execution.

    Defaults to FALSE so a misconfigured account can't accidentally trade
    real coin.
    """
    return os.environ.get("BINANCE_LIVE_ENABLED", "false").lower() == "true"


def _is_testnet(account: dict) -> bool:
    """Decide whether to use testnet for this account.

    Rules (defence-in-depth — any one of these wins → testnet):
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


def _decrypt_creds(account: dict) -> tuple[str, str]:
    """Pull and decrypt API key + secret from a stored account doc."""
    creds = account.get("creds") or {}
    enc_key = creds.get("api_key")
    enc_sec = creds.get("api_secret")
    if not enc_key or not enc_sec:
        raise RuntimeError("Binance account is missing api_key/api_secret")
    return vault_decrypt(enc_key), vault_decrypt(enc_sec)


def _new_exchange(account: dict) -> ccxt.binance:
    """Build a fresh, configured ccxt.binance instance for one operation."""
    api_key, api_secret = _decrypt_creds(account)
    ex = ccxt.binance({
        "apiKey": api_key,
        "secret": api_secret,
        "enableRateLimit": True,
        "options": {"defaultType": "spot"},
    })
    if _is_testnet(account):
        ex.set_sandbox_mode(True)
        logger.debug("Binance instance using TESTNET")
    return ex


class BinanceClient:
    """One-shot async client. Use via `async with` to guarantee close()."""

    def __init__(self, account: dict):
        self.account = account
        self.exchange: Optional[ccxt.binance] = None

    async def __aenter__(self) -> "BinanceClient":
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


# --------------- Lightweight helpers (no live call) ----------------

def normalize_symbol(stoic_symbol: str) -> str:
    """Map our internal symbol to ccxt's `BASE/QUOTE` notation.

    BTCUSD  → BTC/USDT (Binance Spot's USDT-quoted pair)
    ETHUSD  → ETH/USDT
    Already-slashed pairs are returned as-is.
    """
    if not stoic_symbol:
        return ""
    s = stoic_symbol.upper().replace(" ", "")
    if "/" in s:
        return s
    table = {
        "BTCUSD": "BTC/USDT",
        "ETHUSD": "ETH/USDT",
        "SOLUSD": "SOL/USDT",
        "BTCUSDT": "BTC/USDT",
        "ETHUSDT": "ETH/USDT",
    }
    return table.get(s, s)


def is_crypto_symbol(symbol: str) -> bool:
    """True iff this symbol is a crypto pair we can route to Binance."""
    if not symbol:
        return False
    n = normalize_symbol(symbol)
    return "/" in n and any(n.startswith(b + "/") for b in ("BTC", "ETH", "SOL"))
