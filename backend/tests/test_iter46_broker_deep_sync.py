"""iter-46 · On-demand deep broker sync (EA v1.39) — restore & synchronize
STOIC's records with the broker terminal at any time."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(rel):
    return open(os.path.join(BACKEND, rel)).read()


class TestBridgeRoutes:
    def test_poll_trades_dispatches_sync_request(self):
        src = _src("routes/bridge_routes.py")
        assert '"sync_request"' in src
        assert "pending_history_sync" in src
        assert "pending_history_sync.dispatched_at" in src  # re-dispatch guard

    def test_sync_complete_endpoint_exists(self):
        src = _src("routes/bridge_routes.py")
        assert '@router.post("/sync-complete")' in src
        assert "BridgeSyncComplete" in src
        assert "last_full_sync_at" in src
        assert '"broker_sync_complete"' in src  # WS event

    def test_duplicate_deal_repairs_inexact_trades(self):
        src = _src("routes/bridge_routes.py")
        # DuplicateKeyError path must fall through to repair when the matched
        # trade is closed with estimated/unknown/missing exit data.
        assert "needs_repair" in src
        # 'in' deals must always stay a no-op on duplicate
        assert 'if payload.deal_entry == "in":' in src

    def test_heartbeat_auto_heal_queues_sync(self):
        src = _src("routes/bridge_routes.py")
        assert '"requested_by": "auto_heal"' in src
        assert "ghost_check_at" in src  # 5-min throttle

    def test_request_sync_account_endpoint(self):
        src = _src("routes/account_routes.py")
        assert '@router.post("/{account_id}/request-sync")' in src
        assert "7 * 86400" in src  # 7-day default lookback


class TestEaV139:
    def test_version_bumped_everywhere(self):
        ea = _src("static/EmergentTradingBridge.mq5")
        assert '#property version   "1.40"' in ea
        assert '#define EA_CLIENT_VERSION "1.40"' in ea
        assert 'LATEST_EA = "1.40"' in _src("routes/bot_routes.py")
        assert 'LATEST_EA = "1.40"' in _src("routes/diagnostic_routes.py")

    def test_ea_handles_sync_request(self):
        ea = _src("static/EmergentTradingBridge.mq5")
        assert "ParseSyncRequest" in ea
        assert "DeepSyncHistory" in ea
        assert "PushDealById" in ea
        assert "/api/bridge/sync-complete" in ea
        # local 60s re-run guard
        assert "_last_deep_sync" in ea

    def test_frontend_version_matches(self):
        fe = open("/app/frontend/src/pages/Accounts.jsx").read()
        assert 'const LATEST_EA_VERSION = "1.40"' in fe
        assert "request-sync" in fe
        assert "broker_sync_complete" in fe
