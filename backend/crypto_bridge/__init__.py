"""Crypto exchange bridges (Binance, OKX, Bybit…) — server-side REST execution.

Unlike the MT5 Bridge pattern (which relies on a downloadable EA polling a
queue), these brokers expose REST/Websocket APIs we can hit directly. Each
exchange wraps `ccxt.async_support` behind the same `ExecutionEngine`
interface defined in `execution.py`, so the bot_runner / safety_guardian /
signal pipeline are exchange-agnostic.
"""
