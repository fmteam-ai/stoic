"""v1.60.37 scope — ack loosen flag · Wilson interval + n on the Strategy Report · decision badge (×accounts)."""
import os

import pytest

pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _read(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8").read()


def test_ack_that_loosens_the_stored_stop_is_flagged_not_silently_adopted():
    br = _read("backend", "routes", "bridge_routes.py")
    i = br.index("# v1.60.37 — ack loosen flag")
    block = br[i:i + 1600]
    assert '(trade.get("action") == "BUY" and float(actual_sl) < prev_sl)' in block
    assert '(trade.get("action") == "SELL" and float(actual_sl) > prev_sl)' in block
    assert 'update["ack_loosened_stop"] = True' in block and 'push_ops["ack_loosen_events"] = loosen' in block
    assert '"ack_loosened_stop"' in block and "record_event(" in block          # execution-health anomaly event
    assert 'update["stop_loss"] = float(actual_sl)' in block                     # the broker fact is still recorded
    assert 'upd_ops.setdefault("$push", {}).update(push_ops)' in br
    assert '"ack_loosen_events": trade.get("ack_loosen_events") or []' in _read("backend", "loss_postmortem.py")
    lab = _read("frontend", "src", "pages", "LossLab.jsx")
    assert "pm.ack_loosen_events" in lab and "LOOSENED THE STOP" in lab


def test_wilson_interval_and_n_on_every_band_and_engine():
    from strategy_report import build_report, wilson_interval
    assert wilson_interval(0, 0) == (0.0, 0.0)
    lo, hi = wilson_interval(1, 3)
    assert lo == 6.1 and hi == 79.2                                              # 33 % on n=3 is barely informative
    lo, hi = wilson_interval(60, 100)
    assert 50.0 <= lo <= 50.5 and 68.5 <= hi <= 69.5
    dec = [{"engine": "hf_scalp", "symbol": "XAUUSD", "pnl": 10.0 if i % 3 else -5.0, "win": bool(i % 3),
            "mirrored_accounts": 2, "mc_p": 0.45, "range_pos_pct": 5.0, "trend_ride": False, "closed_at": None} for i in range(9)]
    rep = build_report(dec, 30)
    eng = rep["engines"][0]
    assert eng["decisions"] == 9 and eng["trades"] == 18 and eng["win_rate"] == 66.7 and eng["win_rate_ci"] == list(wilson_interval(6, 9))
    band = eng["range_position"][0]
    assert band["band"] == "0–10%" and band["n"] == 9 and band["trades"] == 18 and band["win_rate_ci"] == list(wilson_interval(6, 9))
    fe = _read("frontend", "src", "components", "StrategyReport.jsx")
    assert "r.win_rate_ci[0]}–{r.win_rate_ci[1]}%" in fe and "n={e.decisions}" in fe and "WILSON 95% CI" in fe


def test_postmortem_list_carries_the_decision_badge_and_stop_trails():
    pm = _read("backend", "routes", "postmortem_routes.py")
    i = pm.index("async def list_postmortems")
    block = pm[i:i + 1400]
    assert "group_decisions(docs)" in block and 'row["mirrored_accounts"] = size_by_id.get' in block
    assert 'row["sl_loosen_refusals"]' in block and 'row["ack_loosen_events"]' in block   # the card can finally show them
    lab = _read("frontend", "src", "pages", "LossLab.jsx")
    assert "pm.mirrored_accounts > 1" in lab and "ACCOUNTS · 1 DECISION" in lab


def test_grouping_never_falls_back_to_created_at_and_legacy_postmortems_are_backfilled():
    from decision_groups import entry_time, group_decisions
    legacy = [{"symbol": "XAUUSD", "action": "BUY", "created_at": "2026-10-14T20:00:00+00:00"} for _ in range(3)]
    assert entry_time(legacy[0]) is None and len(group_decisions(legacy)) == 3        # singletons, not one decision
    assert "async def backfill_postmortem_opened_at" in _read("backend", "loss_postmortem.py")
    pm = _read("backend", "routes", "postmortem_routes.py")
    assert pm.count('await backfill_postmortem_opened_at(db, user["id"])') == 2
