"""v1.60.61 #81 — XAUUSD H4 history for gold_trend_pulse (EA 1.66 `h4_history_v1`; 1.65 terminals keep working).

(a) server asks a capable terminal for ≥ 250 completed H4 bars (first poll when short, then daily) via the poll-trades
    `candles_request`; the EA answers with CopyRates(H4) to /api/bridge/candles; H4 payloads accept 400 bars on the broker
    day grid within a 150-day window; (b) H4_MIN_BARS stays 200 and the bias is NONE with a clear reason until warm;
(c) Bot Health "GTP: H4 bars n/200"; (d) Bot Config warns that GTP replaces the preset engine for XAUUSD."""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import gold_h4_history as H  # noqa: E402
from ea_capabilities import capabilities_for  # noqa: E402
from gold_trend_pulse import engine as E  # noqa: E402
from routes.bridge_routes import history_window_sec, payload_cap, validate_bars  # noqa: E402

pytestmark = pytest.mark.unit
ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
NOW = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
OFF = 10800   # broker UTC+3 → MT5 H4 bars open at UTC 21:00, 01:00, 05:00 …


def _h4_bars(n: int, end_ts: int, off: int = OFF, px: float = 4000.0) -> list:
    """n completed H4 bars on the broker grid ending at (not including) end_ts."""
    last_open = ((end_ts + off) // 14400) * 14400 - off - 14400
    return [{"t": last_open - 14400 * k, "o": px, "h": px + 5, "l": px - 5, "c": px + 1, "v": 100} for k in range(n)][::-1]


def _m15_bars(n: int, end_ts: int) -> list:
    last_open = (end_ts // 900) * 900 - 900
    return [{"t": last_open - 900 * k, "o": 4000.0, "h": 4002.0, "l": 3998.0, "c": 4001.0, "v": 10} for k in range(n)][::-1]


class _Coll:
    def __init__(self, docs=None):
        self.docs = list(docs or [])
        self.updates = []

    async def find_one(self, q, proj=None):
        for d in self.docs:
            if all(d.get(k) == v for k, v in q.items()):
                return d
        return None

    async def update_one(self, q, u, upsert=False):
        self.updates.append((q, u))
        for d in self.docs:
            if all(d.get(k) == v for k, v in q.items()):
                d.update(u.get("$set") or {})


class _DB:
    def __init__(self, candles=None, accounts=None):
        self.intraday_candles = _Coll(candles)
        self.accounts = _Coll(accounts)


def _acc(ver="1.66", **extra):
    return {"_id": "acc1", "user_id": "u1", "ea_version": ver, "label": "RoboForex", "status": "connected",
            "broker_time_info": {"server_gmt_offset_sec": OFF}, **extra}


def _m15_doc(**extra):
    return {"user_id": "u1", "symbol": "XAUUSD", "timeframe": "M15", "account_id": "acc1", "t_basis": "utc",
            "updated_at": NOW.isoformat(), "bars": _m15_bars(800, int(NOW.timestamp())), **extra}


def _h4_doc(n=300, backfill_at=None, **extra):
    return {"user_id": "u1", "symbol": "XAUUSD", "timeframe": "H4", "account_id": "acc1", "t_basis": "utc",
            "updated_at": NOW.isoformat(), "backfill_at": backfill_at, "bars": _h4_bars(n, int(NOW.timestamp())), **extra}


# ── capability flag: 1.66 answers, 1.65 keeps working ────────────────────────────────────────────────────────
def test_capability_h4_history_v1_is_1_66_only_and_1_65_unchanged():
    assert "h4_history_v1" in capabilities_for("1.66") and "h4_history_v1" not in capabilities_for("1.65")
    assert {"modify_tp_v1", "m1_stream_v1", "command_fencing_v1", "nl_close_fence_v1"} <= capabilities_for("1.65")
    assert E.H4_MIN_BARS == 200                                   # (b) not relaxed
    assert H.TARGET_BARS >= 250 and H.REQUEST_BARS >= H.TARGET_BARS


# ── (a) when the backfill is due ─────────────────────────────────────────────────────────────────────────────
def test_is_due_short_history_never_backfilled_daily_and_in_flight():
    acc = _acc()
    assert H.is_due(None, acc, NOW) == (True, "0/250 completed H4 bars stored")
    assert H.is_due(_h4_doc(n=120), acc, NOW)[0] is True
    assert H.is_due(_h4_doc(n=300, backfill_at=None), acc, NOW) == (True, "never backfilled")
    fresh = (NOW - timedelta(hours=2)).isoformat()
    assert H.is_due(_h4_doc(n=300, backfill_at=fresh), acc, NOW) == (False, "fresh")
    old = (NOW - timedelta(hours=25)).isoformat()
    assert H.is_due(_h4_doc(n=300, backfill_at=old), acc, NOW) == (True, "daily refresh")
    inflight = _acc(h4_history_request={"dispatched_at": (NOW - timedelta(minutes=3)).isoformat()})
    assert H.is_due(None, inflight, NOW) == (False, "request in flight")
    expired = _acc(h4_history_request={"dispatched_at": (NOW - timedelta(minutes=11)).isoformat()})
    assert H.is_due(None, expired, NOW)[0] is True                # dropped response → re-asked after 10 min


@pytest.mark.asyncio
async def test_pending_request_only_for_capable_gold_streaming_terminals_and_marks_dispatch():
    db = _DB(candles=[_m15_doc()], accounts=[_acc()])
    req = await H.pending_request(db, db.accounts.docs[0], NOW)
    assert req == {"symbol": "XAUUSD", "timeframe": "H4", "bars": H.REQUEST_BARS}
    mark = db.accounts.docs[0]["h4_history_request"]
    assert mark["dispatched_at"] == NOW.isoformat() and mark["why"].startswith("0/250")
    assert await H.pending_request(db, db.accounts.docs[0], NOW + timedelta(minutes=1)) is None   # in flight
    # EA 1.65 → never asked (it would ignore the key anyway)
    db165 = _DB(candles=[_m15_doc()], accounts=[_acc("1.65")])
    assert await H.pending_request(db165, db165.accounts.docs[0], NOW) is None and not db165.accounts.updates
    # terminal that does not stream XAUUSD → not asked
    db_nogold = _DB(candles=[dict(_m15_doc(), symbol="EURUSD")], accounts=[_acc()])
    assert await H.pending_request(db_nogold, db_nogold.accounts.docs[0], NOW) is None
    # complete + fresh → nothing until the daily refresh
    db_ok = _DB(candles=[_m15_doc(), _h4_doc(300, backfill_at=(NOW - timedelta(hours=1)).isoformat())], accounts=[_acc()])
    assert await H.pending_request(db_ok, db_ok.accounts.docs[0], NOW) is None


def test_poll_trades_carries_candles_request_fail_soft_and_ea_1_66_answers_with_copyrates():
    src = open(os.path.join(ROOT, "routes", "bridge_routes.py"), encoding="utf-8").read()
    poll = src.split("async def poll_trades(")[1].split("\n@router")[0]
    assert 'resp["candles_request"] = _h4_req' in poll and "from gold_h4_history import pending_request" in poll
    assert poll.index("from gold_h4_history import") > poll.index('resp["sync_request"]')   # after trades/modifications/sync
    ea = open(os.path.join(ROOT, "static", "EmergentTradingBridge.mq5"), encoding="utf-8").read()
    assert "ParseCandlesRequest(resp);" in ea and '"\\"candles_request\\":{"' in ea
    assert "CopyRates(sym, HistoryPeriod(tf), 1, want, rates)" in ea      # completed bars only (shift 1)
    assert 'if (tf == "D1") return PERIOD_D1;' in ea and "return PERIOD_H4;" in ea
    assert ea.count('"/api/bridge/candles"') == 3                         # same endpoint as the M15 / M1 feeds


# ── ingest: H4 payloads on the broker grid, 150-day window, 400-bar cap ──────────────────────────────────────
def test_h4_ingest_accepts_broker_grid_history_and_intraday_rules_unchanged():
    now_ts = int(datetime.now(timezone.utc).timestamp())
    h4 = _h4_bars(300, now_ts)                                           # UTC t = broker 4-h grid − 3 h
    bars, dropped = validate_bars(h4, "H4", grid_offset_sec=OFF)
    assert len(bars) == 300 and dropped == 0
    assert bars[0]["t"] < now_ts - 45 * 86400                            # 300 × 4 h ≈ 50 days: beyond the intraday ±30-day window
    bars_utc_grid, dropped_utc = validate_bars(h4, "H4")                 # without the offset the broker grid is "misaligned"
    assert dropped_utc == 300 and not bars_utc_grid
    assert payload_cap("H4") == 400 and payload_cap("D1") == 400 and payload_cap("M15") == 200 and payload_cap("M1") == 200
    assert history_window_sec("H4") == 150 * 86400 and history_window_sec("M15") == 30 * 86400
    m15_old = [{"t": (now_ts // 900) * 900 - 40 * 86400, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 1}]
    assert validate_bars(m15_old, "M15") == ([], 1)                      # intraday window still ±30 days
    src = open(os.path.join(ROOT, "routes", "bridge_routes.py"), encoding="utf-8").read()
    recv = src.split("async def receive_candles(")[1].split("\n@router")[0]
    assert 'set_doc["backfill_at"] = now_iso' in recv and "grid_offset_sec=offset if _is_history else 0" in recv


# ── merge: EA H4 bars + M15 resample on one grid ──────────────────────────────────────────────────────────────
def test_merge_h4_ea_bars_win_m15_fills_the_newest_bars_and_m15_only_stays_short():
    now_ts = int(NOW.timestamp())
    m15 = _m15_bars(800, now_ts)
    only_m15, src = H.merge_h4([], m15, OFF, now_ts)
    assert src == "m15_resample" and 45 <= len(only_m15) <= 51 < E.H4_MIN_BARS     # the 50/200 warm-up the user saw
    assert all((b["t"] + OFF) % 14400 == 0 for b in only_m15)                        # broker day grid, like MT5
    stored = _h4_bars(300, now_ts - 3 * 86400, px=3900.0)                           # EA backfill, 3 days old
    merged, src2 = H.merge_h4(stored, m15, OFF, now_ts)
    assert src2 == "ea_h4+m15" and len(merged) >= 300 + 15 and len(merged) >= E.H4_MIN_BARS
    ts = [b["t"] for b in merged]
    assert ts == sorted(ts) and len(set(ts)) == len(ts)                              # ascending, de-duplicated
    assert all(b["t"] + 14400 <= now_ts for b in merged)                             # completed only
    by_t = {b["t"]: b for b in merged}
    assert by_t[stored[-1]["t"]]["o"] == 3900.0                                      # EA bar wins over the resample
    assert by_t[ts[-1]]["o"] == 4000.0                                               # newest bar comes from M15


@pytest.mark.asyncio
async def test_engine_load_bars_is_warm_only_with_the_ea_backfill(monkeypatch):
    import intraday_features as IF
    now_ts = int(NOW.timestamp())
    docs = {"M15": _m15_doc(), "M1": {"bars": [], "t_basis": "utc"}}

    async def fake_load(symbol, user_id, timeframe="M15", last_n=120, max_age_min=30):
        d = docs.get(timeframe)
        if d and d.get("updated_at") and NOW - datetime.fromisoformat(d["updated_at"]) > timedelta(minutes=max_age_min):
            return None                                               # same freshness rule as the real reader
        return d
    monkeypatch.setattr(IF, "load_user_candles", fake_load)

    class _Ticks:
        def find(self, *a, **k):
            return self

        def sort(self, *a, **k):
            return self

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    class _DBx:
        scalp_ticks = _Ticks()
    cold = await E.load_bars(_DBx(), "u1", _acc(), NOW)
    assert cold["h4_warm"] is False and cold["h4_source"] == "m15_resample" and len(cold["h4"]) < E.H4_MIN_BARS
    docs["H4"] = _h4_doc(300)
    warm = await E.load_bars(_DBx(), "u1", _acc(), NOW)
    assert warm["h4_warm"] is True and warm["h4_source"] == "ea_h4+m15" and len(warm["h4"]) >= 300
    docs["H4"] = dict(_h4_doc(300), updated_at=(NOW - timedelta(days=4)).isoformat())    # too old to merge → warm-up again
    assert (await E.load_bars(_DBx(), "u1", _acc(), NOW))["h4_warm"] is False
    eng = open(os.path.join(ROOT, "gold_trend_pulse", "engine.py"), encoding="utf-8").read()
    assert "H4 warm-up: {len(data['h4'])}/{H4_MIN_BARS}" in eng and '"h4_bars": len(data["h4"])' in eng
    assert now_ts  # silence unused


# ── (c) Bot Health row ───────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_bot_health_shows_gtp_h4_bars_n_of_200(monkeypatch):
    import intraday_features as IF
    docs = {"M15": _m15_doc()}

    async def fake_load(symbol, user_id, timeframe="M15", last_n=120, max_age_min=30):
        return docs.get(timeframe)
    monkeypatch.setattr(IF, "load_user_candles", fake_load)
    row = await H.health_check(None, "u1", _acc("1.65"), NOW)
    assert row["label"] == "GTP: H4 bars" and row["status"] == "warn"
    assert "/200 completed XAUUSD H4 bars (m15_resample)" in row["detail"]
    assert "EA 1.65" in row["detail"] and "EA ≥ 1.66" in row["detail"] and row["fix_label"] == "Update the EA to 1.66"
    pending = await H.health_check(None, "u1", _acc("1.66", h4_history_request={"dispatched_at": "2026-06-10T11:58:00+00:00", "why": "0/250"}), NOW)
    assert pending["status"] == "warn" and "requested 2026-06-10T11:58:00+00:00" in pending["detail"]
    docs["H4"] = _h4_doc(300, backfill_at=NOW.isoformat())
    ok = await H.health_check(None, "u1", _acc("1.66"), NOW)
    assert ok["status"] == "pass" and "/200 completed XAUUSD H4 bars (ea_h4+m15)" in ok["detail"] and "last EA backfill" in ok["detail"]
    diag = open(os.path.join(ROOT, "routes", "diagnostic_routes.py"), encoding="utf-8").read()
    assert 'if not tc.get("gold_trend_pulse_enabled"):' in diag and "from gold_h4_history import health_check" in diag


# ── (d) Bot Config warning + docs ────────────────────────────────────────────────────────────────────────────
def test_bot_config_warns_that_gtp_replaces_the_preset_engine_for_xauusd():
    jsx = open(os.path.join(ROOT, "..", "frontend", "src", "pages", "BotConfig.jsx"), encoding="utf-8").read()
    i = jsx.index('data-testid="gtp-replaces-engine-warning"')
    assert jsx.index('data-testid="gtp-enabled-toggle"') < i < jsx.index('data-testid="gtp-mode-select"')
    block = jsx[i:i + 600]
    assert "replaces its preset engine for XAUUSD" in block and "HOLD" in block
    ai = open(os.path.join(ROOT, "ai_signals.py"), encoding="utf-8").read()
    assert "INSTEAD of\n    # the preset engine" in ai or "INSTEAD of" in ai     # the behaviour the warning describes
    notes = open(os.path.join(ROOT, "..", "docs", "RELEASE_NOTES_v1.60.61.md"), encoding="utf-8").read()
    assert "replaces" in notes and "HOLD" in notes and "h4_history_v1" in notes and "1.66" in notes
