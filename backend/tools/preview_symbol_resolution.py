"""Live preview of resolve_broker_symbol() against current account state.
Run: cd /app/backend && python tools/preview_symbol_resolution.py
"""
import asyncio, os, sys
sys.path.insert(0, '/app/backend')
from motor.motor_asyncio import AsyncIOMotorClient
from broker_symbol_detector import resolve_broker_symbol

mongo_url, db_name = 'mongodb://localhost:27017', 'ai_trading_bot'
with open('/app/backend/.env') as f:
    for ln in f:
        if ln.startswith('MONGO_URL='):
            mongo_url = ln.split('=', 1)[1].strip().strip("\"'")
        elif ln.startswith('DB_NAME='):
            db_name = ln.split('=', 1)[1].strip().strip("\"'")


async def main():
    db = AsyncIOMotorClient(mongo_url)[db_name]
    cursor = db.accounts.find({'broker': {'$ne': 'KRAKEN_SPOT'}})
    async for a in cursor:
        broker = a.get('broker')
        avail = a.get('available_symbols')
        user_suf = (a.get('symbol_suffix') or '').strip()
        auto_suf = (a.get('auto_detected_symbol_suffix') or '').strip()
        print(f"\n▸ {broker}  (avail={len(avail) if avail else 0} syms,  user_suffix={user_suf!r},  auto_suffix={auto_suf!r})")
        for base in ('XAUUSD', 'BTCUSD', 'EURUSD'):
            if user_suf:
                resolved, src = base + user_suf, 'manual'
            else:
                resolved = resolve_broker_symbol(base, avail, auto_suf)
                src = 'per_base' if avail else ('auto' if auto_suf else 'bare-fallback')
            marker = '✓' if resolved else '✗ NOT OFFERED — order would be rejected pre-send'
            print(f"   {base:8} → {str(resolved):25} ({src})  {marker}")

asyncio.run(main())
