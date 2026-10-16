"""v1.60.53 — test seam: live tier management reads the BROKER price (trade_manager._broker_snapshot), not
market.get_quote. Legacy tests that fed a web quote wrap it into a fresh broker snapshot."""


def broker_snapshot_from_quote(quote):
    async def _snap(trade):
        q = await quote(trade.get("symbol"))
        return {"price": float(q["price"]), "bid": None, "ask": None, "age_ms": 0, "source": "test",
                "fresh": True, "used": "current_price", "note": ""}
    return _snap
