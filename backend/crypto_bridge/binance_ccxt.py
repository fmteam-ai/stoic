"""Backwards-compat shim — preserves the `binance_ccxt` import path while
all logic now lives in the multi-exchange ``ccxt_engine`` module. New code
should import from ``crypto_bridge.ccxt_engine`` directly.
"""
from crypto_bridge.ccxt_engine import (  # noqa: F401
    BinanceClient,
    CCXTClient,
    normalize_symbol,
    is_crypto_symbol,
    _live_enabled,
    _is_testnet,
    _new_exchange,
    _decrypt_creds,
    EXCHANGES,
    SUPPORTED_EXCHANGES,
    DEFAULT_EXCHANGE_ID,
)
