"""Scalp subsystem · fast-path orchestrator.

Pipeline: regime permission → microstructure setup → calibrated probability
→ expected move → execution cost → conservative net expectancy → risk
approval → final fresh-quote check → order → reconciliation → attribution.

Hot-path guarantees (review items 5/6/7):
- NO awaited Mongo work between signal detection and order decision:
  decision IDs are local uuid4, persistence is fire-and-forget, the account
  document is preloaded on ingest.
- Signal freshness is measured from the INITIATING TICK's receive time,
  not a timestamp minted right before the gate.
- Immediately before submission the newest in-memory quote is rechecked for
  spread and adverse price drift; the EA's slippage veto (entry_price +
  max deviation) is the broker-side final gate.

Cost accounting (review item 1) — ONE consistent convention:
  entry exec  = ask + entry_slip (long)  /  bid − entry_slip (short)
  exit  exec  = executable side; exit slippage applied on STOP/TIMEOUT
                (market-out) but not on TARGET (limit fill)
  net_pips    = signed(exit_exec − entry_exec) − exit_slip_used − commission
  gross_move  = mid-to-mid reference move
Metrics consume outcome.net_pips DIRECTLY — expected costs are never
subtracted a second time.

Label hygiene (review items 10/11): every decision doc carries `dataset`
("candidate" for research labels, live fills are reconciled separately) and
overlapping same-direction candidates are suppressed while a sim is active.
"""
import asyncio
import logging
import os
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scalp import edge, gate, kill, permissions, setup
from scalp import model as scalp_model
from scalp.costs import dynamic_spread_limit
from scalp.features import snapshot
from scalp.forecast import make as make_forecast
from scalp.instruments import approved
from scalp.risk import (ACCOUNT_LIMITS, DEFAULT_LIMITS, RiskState,
                        check as risk_check, check_account)
from scalp.state import ScalpState, TickEvent, now_ms

logger = logging.getLogger("scalp.engine")

EVAL_THROTTLE_MS = 1000
TICK_FLUSH_N = 200
TICK_FLUSH_MS = 10_000
RETRAIN_EVERY_RESOLUTIONS = 200
MAX_DRIFT_BEFORE_SUBMIT_FRAC = 0.5     # of stop distance, ABSOLUTE drift
MAX_BATCH_TRANSPORT_AGE_MS = 3000      # sent_at → arrival; older batches can't trade
AUDIT_BACKLOG_HALT = 500               # pending persist tasks that halt NEW entries
FAILED_ATTEMPT_COST_PIPS = 0.1         # opportunity/ops cost of a rejected order
MIN_FILL_ATTEMPTS_FOR_GATE = 20        # below this the fill-prob prior dominates

# Runner ownership is PROCESS-LOCAL (round 5 item 10): enforcement is a
# distributed account lease (round 7 item 1) — a worker must own
# scalp_owners:{account_id} before processing ticks or applying financials.
_owner_pid = os.getpid()
_worker_id = f"{os.uname().nodename}:{_owner_pid}"
LEASE_TTL_SEC = 30
_lease_cache: dict = {}                # account_id -> (expires_epoch, owned)
_lease_epoch: dict = {}                # account_id -> fencing token we hold
_protection_block: set = set()         # accounts with unprotected positions
_invariant_block: set = set()          # accounts failing invariant checks
_runners: dict = {}
_account_risk: dict = {}               # account_id -> account-wide RiskState
_account_restored: set = set()         # account-level state loaded once
_audit_pending = 0
_audit_failures = 0


def account_risk_state(account_id: str) -> RiskState:
    rs = _account_risk.get(account_id)
    if rs is None:
        rs = RiskState(DEFAULT_LIMITS)
        _account_risk[account_id] = rs
    return rs


def set_protection_block(account_id: str, blocked: bool) -> None:
    """Round 8 — while an account has ANY unprotected open position, new
    scalp entries are refused (flag maintained by protection_guard sweep)."""
    if blocked:
        _protection_block.add(account_id)
    else:
        _protection_block.discard(account_id)


def verify_account_invariants(account_id: str) -> list:
    """Round 8 item 9 — in-memory reconciliation invariants. Violations
    block new entries for the account until a clean pass."""
    violations = []
    runners = runners_for_account(account_id)
    if not runners:
        _invariant_block.discard(account_id)
        return violations
    ars = account_risk_state(account_id)
    open_total = sum(len(r.live_trades) for r in runners)
    if ars.open_scalps != open_total:
        violations.append(
            f"account open count {ars.open_scalps} != live trades {open_total}")
    trade_risk = 0.0
    for r in runners:
        for tid, info in r.live_trades.items():
            trade_risk += float(info.get("stop_risk_usd") or
                                ars.stop_risk_by_trade.get(tid, 0.0))
            if not info.get("stop_px"):
                violations.append(f"open scalp {tid} has no protective stop")
    acct_risk = sum(v for k, v in ars.stop_risk_by_trade.items()
                    if k != "unprotected_positions")
    # round 9 item 9 — tolerance scaled to rounding precision, not 5%
    tol = max(0.01, 0.0001 * max(acct_risk, trade_risk, 1.0))
    if abs(acct_risk - trade_risk) > tol:
        violations.append(
            f"account stop risk ${acct_risk:.2f} != sum of open trade "
            f"stop risks ${trade_risk:.2f} (tol ${tol:.2f})")
    if violations:
        _invariant_block.add(account_id)
        logger.error("scalp invariant violations on %s: %s",
                     account_id, violations)
    else:
        _invariant_block.discard(account_id)
    return violations


async def acquire_account_lease(db, account_id: str,
                                worker_id: str | None = None,
                                ttl_sec: int = LEASE_TTL_SEC) -> bool:
    """Round 7 item 1 — distributed account ownership. Atomically claims
    scalp_owners:{account_id} when this worker already owns it or the lease
    expired. A worker that does NOT own the account must not process ticks,
    create orders or apply financials for it."""
    wid = worker_id or _worker_id
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    lease = {"worker_id": wid,
             "lease_until": (now + timedelta(seconds=ttl_sec)).isoformat(),
             "heartbeat": now_iso}
    # round 8 item 2 — monotonic fencing token: every ownership CHANGE bumps
    # lease_epoch; stale workers carry an older epoch and their persisted
    # writes are rejected (see persist_risk_now).
    res = await db.scalp_owners.update_one(
        {"account_id": account_id, "worker_id": wid},
        {"$set": lease})
    if res.matched_count:                      # renewal — epoch unchanged
        return True
    res = await db.scalp_owners.update_one(
        {"account_id": account_id, "lease_until": {"$lt": now_iso}},
        {"$set": lease, "$inc": {"lease_epoch": 1}})
    if res.matched_count:
        doc = await db.scalp_owners.find_one({"account_id": account_id})
        if doc:
            _lease_epoch[account_id] = int(doc.get("lease_epoch") or 0)
        return True
    doc = await db.scalp_owners.find_one({"account_id": account_id})
    if doc is None:
        await db.scalp_owners.update_one(
            {"account_id": account_id},
            {"$setOnInsert": {**lease, "lease_epoch": 1}}, upsert=True)
        doc = await db.scalp_owners.find_one({"account_id": account_id})
    owned = bool(doc and doc.get("worker_id") == wid)
    if owned and doc:
        _lease_epoch[account_id] = int(doc.get("lease_epoch") or 0)
    return owned


async def ensure_account_lease(db, account_id: str) -> bool:
    """Locally-cached lease check: one DB round-trip per ~half TTL keeps the
    tick hot path free of per-batch database reads."""
    import time as _t
    cached = _lease_cache.get(account_id)
    if cached and _t.time() < cached[0]:
        return cached[1]
    owned = await acquire_account_lease(db, account_id)
    _lease_cache[account_id] = (_t.time() + LEASE_TTL_SEC / 2, owned)
    return owned


async def confirm_account_lease_for_order(db, account_id: str) -> tuple:
    """Round 10 item 1 — NON-CACHED ownership confirmation for order
    execution. The cached helper can return a stale owned=True for up to
    half the lease TTL; a worker that paused past its lease while another
    worker acquired a newer fencing epoch could still submit a broker order
    (fencing protects later DB writes but cannot undo a submitted order).
    This check always reads the ownership record. Returns (owned, epoch)."""
    now = datetime.now(timezone.utc).isoformat()
    doc = await db.scalp_owners.find_one(
        {"account_id": account_id, "worker_id": _worker_id,
         "lease_until": {"$gt": now}},
        {"lease_epoch": 1})
    if not doc:
        return False, 0
    epoch = int(doc.get("lease_epoch") or 0)
    _lease_epoch[account_id] = epoch
    return True, epoch


_service_block_reason: str | None = None


def set_service_block(reason: str | None) -> None:
    """Round 10 item 5 — scalp-critical startup failures (e.g. unique-index
    creation) fail the WHOLE scalp service closed: no new entries anywhere
    until the block is cleared."""
    global _service_block_reason
    _service_block_reason = reason


def service_block_reason() -> str | None:
    return _service_block_reason


async def _restore_account_state(db, account_id: str, force: bool = False):
    """Round 7 item 5 — ONE account-level initialization: load account risk,
    scan ALL open scalp trades once, rebuild monetary stop risk and the open
    count. Symbol runners no longer independently reconstruct the account."""
    if account_id in _account_restored and not force:
        return
    _account_restored.add(account_id)
    ars = account_risk_state(account_id)
    doc = await db.scalp_risk_state.find_one(
        {"account_id": account_id, "symbol": "_ACCOUNT"})
    if doc:
        ars.load_doc(doc)
    open_trades = await db.trades.find(
        {"account_id": account_id, "scope": "scalp_fast",
         "status": "open"}).to_list(100)
    ars.stop_risk_by_trade.clear()
    from pip_utils import pip_value_usd_per_lot_strict
    risk_unknown = False
    for tr in open_trades:
        lot = float(tr.get("lot_size") or 0)
        entry = tr.get("entry_price")
        stop = tr.get("stop_loss")
        cfg = approved(tr.get("symbol") or "")
        pip = cfg.pip_size if cfg else 0.0001
        if lot > 0 and entry and stop:
            # round 10 item 3 — never silently assume $10/pip: an
            # unpriceable symbol marks account risk UNKNOWN and blocks
            # new entries instead of restoring a fabricated number.
            pv = pip_value_usd_per_lot_strict(tr.get("symbol") or "EURUSD")
            if pv is None:
                risk_unknown = True
                logger.error(
                    "risk restore: no authoritative pip value for %s "
                    "(trade %s) — account %s risk marked UNKNOWN, "
                    "entries blocked", tr.get("symbol"), tr["_id"],
                    account_id)
                continue
            ars.add_stop_risk(str(tr["_id"]),
                              lot * abs(float(entry) - float(stop)) / pip * pv)
    if risk_unknown:
        _invariant_block.add(account_id)
    ars.open_scalps = len(open_trades)


class ShadowSim:
    """Barrier-label tracker with explicit gross/cost/net accounting."""

    def __init__(self, decision_id, direction, entry_mid, entry_exec,
                 target_pips, stop_pips, pip, opened_ms, max_holding_ms,
                 exit_slippage_pips=0.0, commission_pips=0.0):
        self.decision_id = decision_id
        self.direction = direction
        self.entry_mid = entry_mid
        self.entry_exec = entry_exec
        self.pip = pip
        self.opened_ms = opened_ms
        self.deadline_ms = opened_ms + max_holding_ms
        self.exit_slippage_pips = exit_slippage_pips
        self.commission_pips = commission_pips
        if direction == "BUY":
            self.target_px = entry_exec + target_pips * pip
            self.stop_px = entry_exec - stop_pips * pip
        else:
            self.target_px = entry_exec - target_pips * pip
            self.stop_px = entry_exec + stop_pips * pip

    def advance(self, t: TickEvent):
        """Long exits at BID, short exits at ASK. Returns outcome dict or None."""
        px = t.bid if self.direction == "BUY" else t.ask
        if self.direction == "BUY":
            if px <= self.stop_px:
                return self._done("stop_first", px, t)
            if px >= self.target_px:
                return self._done("target_first", px, t)
        else:
            if px >= self.stop_px:
                return self._done("stop_first", px, t)
            if px <= self.target_px:
                return self._done("target_first", px, t)
        if t.broker_time_ms >= self.deadline_ms:
            return self._done("timeout", px, t)
        return None

    def _done(self, result, px, t):
        # exit slippage: market-out exits (stop/timeout) slip; limit target doesn't
        exit_slip = self.exit_slippage_pips if result in ("stop_first", "timeout") else 0.0
        signed_exec = ((px - self.entry_exec) if self.direction == "BUY"
                       else (self.entry_exec - px)) / self.pip
        exit_mid = (t.bid + t.ask) / 2.0
        gross = ((exit_mid - self.entry_mid) if self.direction == "BUY"
                 else (self.entry_mid - exit_mid)) / self.pip
        net = signed_exec - exit_slip - self.commission_pips
        return {
            "result": result,
            "gross_move_pips": round(gross, 2),
            "spread_cost_pips": round(abs(self.entry_exec - self.entry_mid) / self.pip
                                      + abs(exit_mid - px) / self.pip, 2),
            "exit_slippage_pips": round(exit_slip, 2),
            "commission_pips": round(self.commission_pips, 2),
            "net_pips": round(net, 2),
            "time_to_exit_ms": int(t.broker_time_ms - self.opened_ms),
            "exit_px": px,
            "resolved_at": datetime.now(timezone.utc).isoformat(),
        }


class ScalpRunner:
    def __init__(self, account_id: str, user_id: str, symbol: str):
        self.account_id = account_id
        self.user_id = user_id
        self.symbol = symbol
        self.cfg = approved(symbol)
        self.state = ScalpState(self.cfg.pip_size)
        self.risk_state = RiskState(DEFAULT_LIMITS)
        self.account_risk = account_risk_state(account_id)
        self._applied_deal_ids: deque = deque(maxlen=500)
        self._financials_applied: deque = deque(maxlen=500)
        self._closed_awaiting_financials: dict = {}
        self.exec_attempts = 0
        self.exec_fills = 0
        self._last_financial_event: dict | None = None
        self.account: dict | None = None       # preloaded on ingest (item 5)
        self.equity = 0.0
        self.mode = "shadow"
        self.enabled = False
        self.commission_usd_per_lot_side = 0.0
        self.broker = ""
        self.account_type = ""
        self.health = {"status": "OK", "open_allowed": False, "reasons": []}
        self.open_sims: list[ShadowSim] = []
        self.live_trades: dict = {}            # trade_id -> {state: OPEN|CLOSE_REQUESTED,...}
        self._hydrated = False
        self._risk_restored = False            # NO entries until restored (item 9)
        self._last_eval_ms = 0
        self._tick_buffer: list = []
        self._last_flush_ms = now_ms()
        self._resolutions_since_retrain = 0
        self.counters = {"ticks": 0, "evals": 0, "candidates": 0,
                         "suppressed_overlap": 0,
                         "shadow_trades": 0, "live_trades": 0, "rejected": 0}
        self.last_decision: dict | None = None

    def model_key(self) -> str:
        return scalp_model.make_key(self.broker, self.account_type, self.symbol)

    # ---------------- ingestion ----------------

    async def ingest(self, db, account: dict, ticks: list[dict],
                     sent_at_ms: int | None) -> dict:
        self.account = account
        self.equity = float(account.get("equity") or 0)
        self.broker = str(account.get("broker") or "")
        self.account_type = str(account.get("account_type") or "")
        recv = now_ms()
        # round 4 items 3/5: transport age known BEFORE processing; missing
        # sender timestamp is UNKNOWN, not fresh — fail closed for entries.
        transport_age = (recv - int(sent_at_ms)) if sent_at_ms else None
        trusted = transport_age is not None and transport_age <= MAX_BATCH_TRANSPORT_AGE_MS
        # round 4 item 4: ordering watermark spans BATCHES, not just this one
        last_tm = (self.state.last_tick.broker_time_ms
                   if self.state.last_tick else None)
        for raw in ticks:
            try:
                tm = int(raw["tm"])
                if last_tm is not None and tm <= last_tm:
                    continue                    # duplicate / out-of-order / old batch
                last_tm = tm
                t = TickEvent(symbol=self.symbol, broker_time_ms=tm,
                              received_time_ms=recv,
                              bid=float(raw["b"]), ask=float(raw["a"]))
            except (KeyError, TypeError, ValueError):
                continue
            self.state.update(t, trusted=trusted)
            self.counters["ticks"] += 1
            self._advance_sims(db, t)
            self._monitor_live_exits(db, t)
            self._tick_buffer.append({"tm": t.broker_time_ms, "b": t.bid, "a": t.ask})
        self.health = kill.evaluate(self.state, self.cfg)
        permissions.maybe_refresh(db, self.user_id, self.symbol, self.cfg)
        self._maybe_flush_ticks(db)
        # Delayed-batch guard (round 3): old broker ticks arriving NOW must
        # not look fresh. New entries require BOTH recent transport AND a
        # recent broker-market timestamp (clock-offset corrected).
        broker_age = self.state.broker_adjusted_age_ms()
        batch_fresh = (trusted and broker_age <= self.cfg.max_quote_age_ms)
        if not batch_fresh:
            self.counters["stale_batches"] = self.counters.get("stale_batches", 0) + 1
        audit_ok = _audit_pending < AUDIT_BACKLOG_HALT
        if self.enabled and self._risk_restored and batch_fresh and audit_ok:
            await self._maybe_evaluate(db)
        return {"ok": True, "ticks": len(ticks),
                "health": self.health["status"], "enabled": self.enabled,
                "risk_restored": self._risk_restored,
                "batch_fresh": batch_fresh,
                "transport_age_ms": transport_age,
                "trusted": trusted,
                "broker_age_ms": broker_age}

    # ---------------- decision pipeline ----------------

    def _commission_pips(self, lot_neutral: bool = True) -> float:
        if self.commission_usd_per_lot_side <= 0:
            return 0.0
        from pip_utils import pip_value_usd_per_lot
        pv = pip_value_usd_per_lot(self.symbol, None) or 10.0
        return round(self.commission_usd_per_lot_side * 2.0 / pv, 3)

    async def _maybe_evaluate(self, db):
        nm = now_ms()
        if nm - self._last_eval_ms < EVAL_THROTTLE_MS:
            return
        self._last_eval_ms = nm
        self.counters["evals"] += 1

        feats = snapshot(self.state)
        if feats is None:
            return
        perms = permissions.get_cached(self.user_id, self.symbol)
        cand = setup.detect(feats, self.state)
        if cand is None:
            return
        direction = cand["direction"]
        # item 3 (round 3) — ONE active setup event per symbol: any live sim
        # (either direction) suppresses new labels against the same path
        if self.open_sims:
            self.counters["suppressed_overlap"] += 1
            return
        self.counters["candidates"] += 1
        perm_ok = (perms.get("long_enabled") if direction == "BUY"
                   else perms.get("short_enabled"))

        commission_pips = self._commission_pips()
        pred = scalp_model.predict(self.model_key(), feats)
        model_p = pred["p"]
        fc = make_forecast(feats, cand, self.state, self.cfg,
                           model_p=model_p, commission_pips=commission_pips)
        edge_res = edge.evaluate(fc)

        from pip_utils import pip_value_usd_per_lot
        pip_val = pip_value_usd_per_lot(self.symbol, None)
        # round 6 items 4/5 — EXPLICIT account-level policy: open-position
        # count AND monetary stop-risk budget across ALL runners.
        # round 8 — protection/invariant blocks veto entries outright.
        account_open = sum(len(r.live_trades) for r in runners_for_account(self.account_id))
        if _service_block_reason:
            risk_res = {"ok": False, "lot": 0.0,
                        "reason": f"scalp service blocked: {_service_block_reason}"}
        elif self.account_id in _protection_block:
            risk_res = {"ok": False, "lot": 0.0,
                        "reason": ("unprotected position on account — new "
                                   "scalp entries blocked until protection "
                                   "is confirmed")}
        elif self.account_id in _invariant_block:
            risk_res = {"ok": False, "lot": 0.0,
                        "reason": ("reconciliation invariant violation — new "
                                   "scalp entries blocked until state is "
                                   "consistent")}
        elif account_open >= ACCOUNT_LIMITS.max_total_open_positions:
            risk_res = {"ok": False, "lot": 0.0,
                        "reason": "account-level max open scalp positions"}
        else:
            risk_res = risk_check(self.risk_state, self.equity, fc.stop_pips,
                                  pip_val, self.cfg)
            if risk_res["ok"]:
                proposed_stop_risk = (risk_res["lot"] * fc.stop_pips
                                      * (pip_val or 10.0))
                acct_check = check_account(
                    self.account_risk, self.equity,
                    proposed_stop_risk_usd=proposed_stop_risk)
                if not acct_check["ok"]:
                    risk_res = {"ok": False, "lot": 0.0,
                                "reason": acct_check["reason"]}
        spread_limit = dynamic_spread_limit(self.state, self.cfg)
        # item 6 — freshness measured from the INITIATING tick
        signal_ts_ms = self.state.last_tick.received_time_ms
        gate_res = gate.final_execution_gate(
            self.state, self.cfg, edge_res["net_edge_pips"],
            signal_ts_ms=signal_ts_ms, spread_limit_pips=spread_limit,
            health_open_allowed=self.health["open_allowed"])

        all_ok = bool(perm_ok) and edge_res["ok"] and risk_res["ok"] and gate_res["ok"]
        if all_ok and self.mode == "demo_live":
            verdict = "live_traded"
        elif all_ok:
            verdict = "shadow_traded"
        else:
            verdict = "rejected"
            self.counters["rejected"] += 1

        t = self.state.last_tick
        entry_mid = (t.bid + t.ask) / 2.0
        entry_exec = ((t.ask + fc.expected_slippage_pips * self.cfg.pip_size)
                      if direction == "BUY"
                      else (t.bid - fc.expected_slippage_pips * self.cfg.pip_size))

        decision_id = uuid.uuid4().hex           # item 5 — local ID, no DB wait
        doc = {
            "decision_id": decision_id,
            "user_id": self.user_id, "account_id": self.account_id,
            "symbol": self.symbol,
            "broker": self.broker, "account_type": self.account_type,
            "model_key": self.model_key(),
            "dataset": "candidate",
            "ts_ms": nm, "signal_ts_ms": signal_ts_ms,
            "feature_snapshot_ts_ms": feats["_now_ms"],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "direction": direction, "mode": self.mode,
            "features": {k: v for k, v in feats.items() if not k.startswith("_")},
            "setup": cand, "forecast": fc.to_dict(),
            "model_source": pred["source"],
            "model_fallback_reason": pred["fallback_reason"],
            "net_edge_pips": edge_res["net_edge_pips"],
            "cost_pips": edge_res["cost_pips"],
            "cost_ratio": edge_res["cost_ratio"],
            "gates": {
                "permission": {"ok": bool(perm_ok), "regime": perms.get("regime"),
                               "reasons": perms.get("reasons", [])},
                "edge": edge_res, "risk": risk_res, "final": gate_res,
            },
            "verdict": verdict,
            "lot": risk_res.get("lot", 0.0),
            "sim": {"entry_mid": entry_mid, "entry_exec": entry_exec,
                    "target_pips": fc.target_pips, "stop_pips": fc.stop_pips},
            "outcome": None,
        }
        _bg(lambda d=dict(doc): db.scalp_decisions.insert_one(dict(d)),
            "decision_insert")
        self.last_decision = doc

        self.open_sims.append(ShadowSim(
            decision_id, direction, entry_mid, entry_exec,
            fc.target_pips, fc.stop_pips, self.cfg.pip_size, t.broker_time_ms,
            self.risk_state.limits.max_holding_ms,
            exit_slippage_pips=fc.expected_slippage_pips,
            commission_pips=commission_pips))
        if verdict == "shadow_traded":
            self.counters["shadow_trades"] += 1
        if verdict == "live_traded":
            await self._submit_live(db, doc, fc, risk_res)

    async def _submit_live(self, db, decision: dict, fc, risk_res):
        """Order via the bridge queue. Account is PRELOADED (item 5); the
        freshest in-memory quote is rechecked for spread + drift (item 7);
        the EA's entry_price slippage veto is the broker-side last gate."""
        t = self.state.last_tick
        pip = self.cfg.pip_size
        # ABSOLUTE price-drift guard (round 3 item 7): favorable drift also
        # invalidates the forecast geometry — reject either way, the next
        # evaluation cycle re-forecasts from the new price state.
        ref = decision["sim"]["entry_mid"]
        cur_mid = (t.bid + t.ask) / 2.0
        drift = abs(cur_mid - ref) / pip
        if drift > MAX_DRIFT_BEFORE_SUBMIT_FRAC * fc.stop_pips:
            self.state.record_reject()
            _bg(lambda s={"verdict": "rejected", "reject_stage": "pre_submit_drift",
                          "drift_pips": round(drift, 2)}: db.scalp_decisions.update_one(
                {"decision_id": decision["decision_id"]}, {"$set": s}),
                "decision_update")
            return
        sp = self.state.spread_pips()
        if sp is None or sp > dynamic_spread_limit(self.state, self.cfg):
            self.state.record_reject()
            _bg(lambda s={"verdict": "rejected", "reject_stage": "pre_submit_spread"}: db.scalp_decisions.update_one(
                {"decision_id": decision["decision_id"]}, {"$set": s}),
                "decision_update")
            return
        entry = t.ask if decision["direction"] == "BUY" else t.bid
        # round 5 item 8 — attempt-level EV: EV = P(fill)·EV(filled) − C(fail)
        fs = self.fill_stats()
        ev_attempt = (fs["p_fill"] * decision["net_edge_pips"]
                      - (1 - fs["p_fill"]) * FAILED_ATTEMPT_COST_PIPS)
        if fs["attempts"] >= MIN_FILL_ATTEMPTS_FOR_GATE and ev_attempt <= 0:
            self.state.record_reject()
            _bg(lambda s={"verdict": "rejected",
                          "reject_stage": "low_fill_probability_ev",
                          "fill_stats": fs,
                          "ev_attempt_pips": round(ev_attempt, 3)}: db.scalp_decisions.update_one(
                {"decision_id": decision["decision_id"]}, {"$set": s}),
                "decision_update")
            return
        if decision["direction"] == "BUY":
            sl, tp = entry - fc.stop_pips * pip, entry + fc.target_pips * pip
        else:
            sl, tp = entry + fc.stop_pips * pip, entry - fc.target_pips * pip
        from execution import for_account as engine_for_account
        account = self.account
        if not account:
            return
        engine = engine_for_account(account)
        # round 9 item 4 / round 10 item 1 — re-confirm account ownership at
        # the LAST moment before broker submission with a NON-CACHED read;
        # the cached helper could return stale owned=True after another
        # worker acquired a newer fencing epoch.
        owned, lease_epoch = await confirm_account_lease_for_order(
            db, self.account_id)
        if not owned:
            self.state.record_reject()
            _bg(lambda: db.scalp_decisions.update_one(
                {"decision_id": decision["decision_id"]},
                {"$set": {"verdict": "rejected",
                          "reject_stage": "lease_lost_before_submit"}}),
                "decision_update")
            return
        submit_ms = now_ms()
        trade = await engine.execute(
            user_id=self.user_id, account=account,
            signal={"symbol": self.symbol, "action": decision["direction"],
                    "lot_size": risk_res["lot"],
                    "entry_price": round(entry, 5),
                    "stop_loss": round(sl, 5), "take_profit": round(tp, 5),
                    "origin": "auto", "scope": "scalp_fast",
                    "scalp_decision_id": decision["decision_id"],
                    "scalp_lease_epoch": lease_epoch},
            cfg_account_id=self.account_id)
        if trade.get("blocked"):
            self.state.record_reject()
            # round 5 item 8 — rejected attempts are OUTCOMES: they feed the
            # empirical fill-probability model, not just an audit trail.
            _bg(lambda s={"verdict": "rejected", "reject_stage": "broker_blocked",
                          "dataset": "attempt_failed",
                          "submission_result": trade.get("reason"),
                          "block_reason": trade.get("reason")}: db.scalp_decisions.update_one(
                {"decision_id": decision["decision_id"]}, {"$set": s}),
                "decision_update")
            return
        tid = str(trade.get("id") or trade.get("_id") or "")
        self.risk_state.record_open()
        self.account_risk.record_open()
        self._persist_risk(db)
        self.counters["live_trades"] += 1
        # estimated execution cost in USD for the daily cost budget (item 13)
        from pip_utils import pip_value_usd_per_lot
        pv = pip_value_usd_per_lot(self.symbol, None) or 10.0
        est_cost_usd = ((sp or 0) + 2 * fc.expected_slippage_pips) * pv * risk_res["lot"] \
            + self.commission_usd_per_lot_side * 2 * risk_res["lot"]
        # round 6 item 5 — register this position's monetary stop risk
        stop_risk_usd = risk_res["lot"] * fc.stop_pips * pv
        self.account_risk.add_stop_risk(tid, stop_risk_usd)
        self.live_trades[tid] = {
            "state": "OPEN",
            "opened_ms": now_ms(), "direction": decision["direction"],
            "lot": risk_res["lot"],
            "entry_px": entry, "stop_px": sl, "target_px": tp,
            "stop_risk_usd": round(stop_risk_usd, 2),
            "decision_id": decision["decision_id"],
            "order_submit_ts_ms": submit_ms,
            "est_cost_usd": round(est_cost_usd, 2),
        }
        _bg(lambda s={"dataset": "submitted", "trade_id": tid,
                      "order_submit_ts_ms": submit_ms}: db.scalp_decisions.update_one(
            {"decision_id": decision["decision_id"]}, {"$set": s}),
            "decision_update")

    # ---------------- exits & sims ----------------

    def _advance_sims(self, db, t: TickEvent):
        still = []
        for sim in self.open_sims:
            out = sim.advance(t)
            if out is None:
                still.append(sim)
                continue
            self._resolutions_since_retrain += 1
            _bg(lambda d=sim.decision_id, o=out: db.scalp_decisions.update_one(
                {"decision_id": d}, {"$set": {"outcome": o}}), "sim_outcome")
        self.open_sims = still
        if self._resolutions_since_retrain >= RETRAIN_EVERY_RESOLUTIONS:
            self._resolutions_since_retrain = 0
            _bg(lambda: scalp_model.retrain(db, self.symbol,
                                            broker=self.broker,
                                            account_type=self.account_type),
                "model_retrain")

    def _monitor_live_exits(self, db, t: TickEvent):
        """Item 14 — positions stay monitored through CLOSE_REQUESTED until
        the broker confirms the close via /bridge/report."""
        if not self.live_trades:
            return
        nm = now_ms()
        sp = self.state.spread_pips() or 0.0
        for tid, info in self.live_trades.items():
            if info.get("state") != "OPEN":
                continue
            reason = None
            if nm - info["opened_ms"] >= self.risk_state.limits.max_holding_ms:
                reason = "max_holding_time"
            elif sp >= 3.0 * self.cfg.max_spread_pips:
                reason = "spread_shock"
            elif self.health["status"] != "OK" and not self.health["open_allowed"]:
                reason = "execution_degraded"
            if reason:
                info["state"] = "CLOSE_REQUESTED"
                info["close_requested_ms"] = nm
                # round 5 item 9 — exact exit-slippage baseline captured at
                # close request: reference quotes + requested exit price.
                lt = self.state.last_tick
                if lt is not None:
                    info["exit_reference_bid"] = lt.bid
                    info["exit_reference_ask"] = lt.ask
                    info["requested_exit_price"] = (
                        lt.bid if info.get("direction") == "BUY" else lt.ask)
                _bg(lambda t=tid, rs=reason: self._request_close(db, t, rs),
                    "request_close")

    async def _request_close(self, db, trade_id: str, reason: str):
        await db.trades.update_one(
            {"_id": _oid(trade_id), "status": "open"},
            {"$set": {"pending_modification": {
                "type": "FULL_CLOSE",
                "requested_at": datetime.now(timezone.utc).isoformat(),
                "reason": f"scalp_{reason}"},
                "close_reason": f"scalp_{reason}"}})

    def on_trade_opened(self, trade_id: str, requested_price: float | None,
                        actual_price: float | None, db=None):
        """Broker fill confirmation (round 3 item 5): feed REAL entry slippage
        back into the state so the cost model learns from live fills."""
        info = self.live_trades.get(trade_id)
        if info is not None and not info.get("fill_counted"):
            info["fill_counted"] = True
            self.exec_fills += 1
        if requested_price and actual_price:
            direction = (info or {}).get("direction", "BUY")
            signed = ((actual_price - requested_price) if direction == "BUY"
                      else (requested_price - actual_price)) / self.cfg.pip_size
            self.state.record_fill(signed)
            if info is not None:
                info["requested_entry"] = requested_price
                info["actual_entry"] = actual_price
                info["entry_slippage_pips"] = round(signed, 2)
            if db is not None and info:
                _bg(lambda: db.scalp_decisions.update_one(
                    {"decision_id": info.get("decision_id", "")},
                    {"$set": {"requested_entry": requested_price,
                              "actual_entry": actual_price,
                              "entry_slippage_pips": round(signed, 2)}}),
                    "entry_fill")

    def on_close_ack(self, trade_id: str, exit_price: float | None = None,
                     db=None):
        """Operational close acknowledgement (/bridge/report). Frees the
        position slot and stops exit monitoring — but applies NO financials.
        /bridge/external-deal is the authoritative reconciliation source
        (round 5 item 1); the info dict is parked until its deal arrives."""
        if trade_id in self._financials_applied:
            return
        info = self.live_trades.pop(trade_id, None)
        if info is None:
            return
        info["close_ack_ms"] = now_ms()
        if exit_price:
            info["ack_exit_price"] = exit_price
        self._closed_awaiting_financials[trade_id] = info
        if len(self._closed_awaiting_financials) > 200:
            self._closed_awaiting_financials.pop(
                next(iter(self._closed_awaiting_financials)))
        self.risk_state.record_close()
        self.account_risk.record_close()
        self.account_risk.remove_stop_risk(trade_id)
        if db is not None:
            self._persist_risk(db)

    def on_trade_closed(self, trade_id: str, pnl: float,
                        commission: float = 0.0, swap: float = 0.0,
                        exit_price: float | None = None,
                        deal_id: str | None = None,
                        close_reason: str | None = None,
                        source: str = "report", db=None):
        """Authoritative financial reconciliation (round 5 items 1/2) —
        normally fed by /bridge/external-deal with SIGNED broker figures.
        Idempotent per deal_id AND per trade_id: financials apply exactly
        once even when both reporting paths fire.

        SIGNED MT5 semantics: net = profit + commission + swap. abs() would
        erase positive-swap credits and commission rebates."""
        if deal_id is not None and deal_id in self._applied_deal_ids:
            return
        if trade_id in self._financials_applied:
            return
        info = self.live_trades.pop(trade_id, None)
        if info is not None:
            self.risk_state.record_close()
            self.account_risk.record_close()
        else:
            info = self._closed_awaiting_financials.pop(trade_id, None)
        self.account_risk.remove_stop_risk(trade_id)
        if deal_id is not None:
            self._applied_deal_ids.append(deal_id)
        self._financials_applied.append(trade_id)
        pnl = float(pnl or 0)
        commission = float(commission or 0)
        swap = float(swap or 0)
        net_pnl = pnl + commission + swap
        # round 6 item 9 — loss streak uses TRADING P&L: market result plus
        # commission plus negative financing, EXCLUDING financing credits.
        trading_pnl = pnl + commission + min(0.0, swap)
        execution_cost = max(0.0, -commission) + max(0.0, -swap)
        financing_credit = max(0.0, swap)
        est_cost = float(info.get("est_cost_usd", 0.0)) if info else 0.0
        broker_costs_known = (commission != 0.0 or swap != 0.0
                              or source == "broker_deal")
        cost_used = execution_cost if broker_costs_known else est_cost
        self.risk_state.record_result(net_pnl, cost_used,
                                      trading_pnl_usd=trading_pnl)
        self.account_risk.record_result(net_pnl, cost_used,
                                        trading_pnl_usd=trading_pnl)
        # round 6 item 8 — exit-slippage baseline: a CONFIRMED stop-loss exit
        # ALWAYS measures against the stored stop price (gap-through-stop is
        # exactly where slippage matters most). Otherwise use the close-request
        # quote, falling back to the stop level only for near-stop fills.
        exit_slip = None
        req_exit = (info or {}).get("requested_exit_price")
        is_stop = bool(close_reason and "stop_loss" in str(close_reason))
        if info and info.get("stop_px") and (is_stop or (
                req_exit is None and exit_price
                and abs(exit_price - float(info["stop_px"])) <= 3 * self.cfg.pip_size)):
            req_exit = float(info["stop_px"])
        if info and exit_price and req_exit:
            d = info.get("direction", "BUY")
            adverse = ((req_exit - exit_price) if d == "BUY"
                       else (exit_price - req_exit)) / self.cfg.pip_size
            exit_slip = round(adverse, 2)
        if db is not None:
            self._persist_risk(db)
            # round 8 item 3 — immutable financial-event ledger (replayable
            # audit trail; risk snapshots stay the fast-path cache)
            ev = {"account_id": self.account_id, "symbol": self.symbol,
                  "trade_id": trade_id, "deal_id": deal_id,
                  "event_type": "full_close",
                  "net_pnl": round(net_pnl, 2),
                  "trading_pnl": round(trading_pnl, 2),
                  "execution_cost": round(cost_used, 2),
                  "commission": commission, "swap": swap,
                  "risk_applied": True, "lease_epoch": _lease_epoch.get(self.account_id, 0),
                  "at": datetime.now(timezone.utc).isoformat()}
            self._last_financial_event = ev
            _bg(lambda e=ev: db.scalp_financial_events.update_one(
                {"account_id": e["account_id"], "deal_id": e["deal_id"],
                 "event_type": e["event_type"]},
                {"$setOnInsert": e}, upsert=True), "financial_event")
            if info:
                outcome = {
                    "requested_entry_price": info.get("requested_entry"),
                    "actual_entry_price": info.get("actual_entry", info.get("entry_px")),
                    "entry_slippage_pips": info.get("entry_slippage_pips"),
                    "requested_exit_price": req_exit,
                    "actual_exit_price": exit_price,
                    "exit_reference_bid": info.get("exit_reference_bid"),
                    "exit_reference_ask": info.get("exit_reference_ask"),
                    "exit_slippage_pips": exit_slip,
                    "exit_reason": close_reason,
                    "close_request_ts_ms": info.get("close_requested_ms"),
                    "gross_market_pnl_usd": pnl,
                    "commission_usd": commission,          # SIGNED
                    "swap_usd": swap,                      # SIGNED
                    "net_pnl_usd": round(net_pnl, 2),
                    "trading_pnl_usd": round(trading_pnl, 2),
                    "execution_cost_usd": round(execution_cost, 2),
                    "financing_credit_usd": round(financing_credit, 2),
                    "execution_cost_usd_used": round(cost_used, 2),
                    "cost_source": "broker" if broker_costs_known else "estimated",
                    "broker_deal_id": deal_id,
                }
                _bg(lambda o=outcome: db.scalp_decisions.update_one(
                    {"decision_id": info.get("decision_id", "")},
                    {"$set": {"dataset": "filled_live",
                              "execution_outcome": o,
                              "live_pnl_usd": o["net_pnl_usd"],
                              "live_cost_usd": o["execution_cost_usd_used"]}}),
                    "live_close")

    def on_partial_close(self, trade_id: str, closed_lots: float,
                         remaining_lots: float, pnl: float,
                         commission: float = 0.0, swap: float = 0.0,
                         exit_price: float | None = None,
                         deal_id: str | None = None, db=None):
        """Round 6 item 1 — partial closes hit the SAME financial budgets as
        full closes: realized P&L, signed costs, streak (trading P&L), and
        the tracked lot/stop-risk shrink. The position slot stays OPEN —
        record_close() only fires when the final volume reaches zero."""
        if deal_id is not None and deal_id in self._applied_deal_ids:
            return
        if trade_id in self._financials_applied:
            return
        if deal_id is not None:
            self._applied_deal_ids.append(deal_id)
        pnl = float(pnl or 0)
        commission = float(commission or 0)
        swap = float(swap or 0)
        net_pnl = pnl + commission + swap
        trading_pnl = pnl + commission + min(0.0, swap)
        execution_cost = max(0.0, -commission) + max(0.0, -swap)
        self.risk_state.record_result(net_pnl, execution_cost,
                                      trading_pnl_usd=trading_pnl)
        self.account_risk.record_result(net_pnl, execution_cost,
                                        trading_pnl_usd=trading_pnl)
        info = self.live_trades.get(trade_id)
        if info is not None:
            prior_lot = float(info.get("lot") or 0)
            info["lot"] = max(0.0, float(remaining_lots or 0))
            # round 8 item 6 — EXACT remaining stop risk from the broker's
            # remaining volume and the stored stop, not proportional scaling
            entry, stop = info.get("entry_px"), info.get("stop_px")
            if info["lot"] > 0 and entry and stop:
                from pip_utils import pip_value_usd_per_lot
                pv = pip_value_usd_per_lot(self.symbol, None) or 10.0
                self.account_risk.add_stop_risk(
                    trade_id, info["lot"] * abs(float(entry) - float(stop))
                    / self.cfg.pip_size * pv)
            elif prior_lot > 0 and info["lot"] > 0:
                self.account_risk.scale_stop_risk(trade_id,
                                                  info["lot"] / prior_lot)
            elif info["lot"] <= 0:
                self.account_risk.remove_stop_risk(trade_id)
            info.setdefault("partial_exits", []).append({
                "deal_id": deal_id, "closed_lots": float(closed_lots or 0),
                "exit_price": exit_price, "net_pnl_usd": round(net_pnl, 2),
                "ts_ms": now_ms()})
        if db is not None:
            self._persist_risk(db)
            ev = {"account_id": self.account_id, "symbol": self.symbol,
                  "trade_id": trade_id, "deal_id": deal_id,
                  "event_type": "partial_close",
                  "net_pnl": round(net_pnl, 2),
                  "trading_pnl": round(trading_pnl, 2),
                  "execution_cost": round(execution_cost, 2),
                  "commission": commission, "swap": swap,
                  "remaining_lots": float(remaining_lots or 0),
                  "risk_applied": True, "status": "applied",
                  "lease_epoch": _lease_epoch.get(self.account_id, 0),
                  "at": datetime.now(timezone.utc).isoformat()}
            self._last_financial_event = ev
            _bg(lambda e=ev: db.scalp_financial_events.update_one(
                {"account_id": e["account_id"], "deal_id": e["deal_id"],
                 "event_type": e["event_type"]},
                {"$setOnInsert": e}, upsert=True), "financial_event")
            if info and info.get("decision_id"):
                rec = {"deal_id": deal_id,
                       "closed_lots": float(closed_lots or 0),
                       "remaining_lots": float(remaining_lots or 0),
                       "exit_price": exit_price,
                       "gross_market_pnl_usd": pnl,
                       "commission_usd": commission, "swap_usd": swap,
                       "net_pnl_usd": round(net_pnl, 2),
                       "at": datetime.now(timezone.utc).isoformat()}
                _bg(lambda r=rec: db.scalp_decisions.update_one(
                    {"decision_id": info["decision_id"]},
                    {"$push": {"partial_exit_records": r}}), "partial_close")

    # ---------------- risk-state persistence (item 9) ----------------

    async def persist_risk_now(self, db):
        """Round 7 item 6 — SYNCHRONOUS risk + deal-id persistence, awaited
        BEFORE a broker deal may be marked reconciliation-complete.
        Round 8 item 2 — epoch-fenced: a stale worker (older lease_epoch)
        cannot overwrite state persisted by the current owner."""
        now_iso = datetime.now(timezone.utc).isoformat()
        epoch = _lease_epoch.get(self.account_id, 0)
        fence = {"$or": [{"lease_epoch": {"$exists": False}},
                         {"lease_epoch": {"$lte": epoch}}]}
        res = await db.scalp_risk_state.update_one(
            {"account_id": self.account_id, "symbol": self.symbol, **fence},
            {"$set": {"user_id": self.user_id, **self.risk_state.to_doc(),
                      "applied_deal_ids": list(self._applied_deal_ids),
                      "lease_epoch": epoch, "saved_at": now_iso}}, upsert=False)
        if not res.matched_count:
            existing = await db.scalp_risk_state.find_one(
                {"account_id": self.account_id, "symbol": self.symbol},
                {"lease_epoch": 1})
            # Round 10 fix (iter-45) — stale ONLY when the existing doc holds
            # a strictly NEWER epoch. A doc with our own (or older) epoch
            # means we lost an insert race against our own background
            # _persist_risk between update_one and find_one: retry the
            # fenced write instead of falsely failing the reconciliation.
            if existing is not None and int(existing.get("lease_epoch") or 0) > epoch:
                raise RuntimeError(
                    f"risk persist fenced out (stale lease epoch {epoch} < "
                    f"{existing.get('lease_epoch')})")
            if existing is not None:
                await db.scalp_risk_state.update_one(
                    {"account_id": self.account_id, "symbol": self.symbol,
                     **fence},
                    {"$set": {"user_id": self.user_id,
                              **self.risk_state.to_doc(),
                              "applied_deal_ids": list(self._applied_deal_ids),
                              "lease_epoch": epoch, "saved_at": now_iso}},
                    upsert=False)
            else:
                await db.scalp_risk_state.update_one(
                    {"account_id": self.account_id, "symbol": self.symbol},
                    {"$setOnInsert": {"user_id": self.user_id,
                                      **self.risk_state.to_doc(),
                                      "applied_deal_ids": list(self._applied_deal_ids),
                                      "lease_epoch": epoch, "saved_at": now_iso}},
                    upsert=True)
        await self._fenced_account_write(db, epoch, now_iso)

    async def _fenced_account_write(self, db, epoch: int, now_iso: str):
        """Round 9 critical — the shared _ACCOUNT doc is fenced too."""
        fence = {"$or": [{"lease_epoch": {"$exists": False}},
                         {"lease_epoch": {"$lte": epoch}}]}
        res = await db.scalp_risk_state.update_one(
            {"account_id": self.account_id, "symbol": "_ACCOUNT", **fence},
            {"$set": {"user_id": self.user_id, **self.account_risk.to_doc(),
                      "lease_epoch": epoch, "saved_at": now_iso}},
            upsert=False)
        if not res.matched_count:
            existing = await db.scalp_risk_state.find_one(
                {"account_id": self.account_id, "symbol": "_ACCOUNT"},
                {"lease_epoch": 1})
            # Round 10 fix (iter-45) — same epoch-aware rejection as
            # persist_risk_now: only a strictly newer epoch is stale.
            if existing is not None and int(existing.get("lease_epoch") or 0) > epoch:
                raise RuntimeError(
                    f"account risk persist fenced out (stale epoch {epoch} < "
                    f"{existing.get('lease_epoch')})")
            if existing is not None:
                await db.scalp_risk_state.update_one(
                    {"account_id": self.account_id, "symbol": "_ACCOUNT",
                     **fence},
                    {"$set": {"user_id": self.user_id,
                              **self.account_risk.to_doc(),
                              "lease_epoch": epoch, "saved_at": now_iso}},
                    upsert=False)
            else:
                await db.scalp_risk_state.update_one(
                    {"account_id": self.account_id, "symbol": "_ACCOUNT"},
                    {"$setOnInsert": {"user_id": self.user_id,
                                      **self.account_risk.to_doc(),
                                      "lease_epoch": epoch, "saved_at": now_iso}},
                    upsert=True)

    def _persist_risk(self, db):
        """Async telemetry mirror of the risk snapshots — FENCED like the
        synchronous path (round 9): a stale-epoch worker's background write
        silently loses instead of clobbering the owner's state."""
        epoch = _lease_epoch.get(self.account_id, 0)
        fence = {"$or": [{"lease_epoch": {"$exists": False}},
                         {"lease_epoch": {"$lte": epoch}}]}
        _bg(lambda: db.scalp_risk_state.update_one(
            {"account_id": self.account_id, "symbol": self.symbol, **fence},
            {"$set": {"user_id": self.user_id, **self.risk_state.to_doc(),
                      "applied_deal_ids": list(self._applied_deal_ids),
                      "lease_epoch": epoch,
                      "saved_at": datetime.now(timezone.utc).isoformat()}},
            upsert=True), "risk_state")
        _bg(lambda: db.scalp_risk_state.update_one(
            {"account_id": self.account_id, "symbol": "_ACCOUNT", **fence},
            {"$set": {"user_id": self.user_id, **self.account_risk.to_doc(),
                      "lease_epoch": epoch,
                      "saved_at": datetime.now(timezone.utc).isoformat()}},
            upsert=True), "account_risk_state")

    async def restore_risk(self, db):
        doc = await db.scalp_risk_state.find_one(
            {"account_id": self.account_id, "symbol": self.symbol})
        if doc:
            self.risk_state.load_doc(doc)
            # round 6 item 6 — durable deal-id idempotency cache survives
            # restarts; the broker_deals reconciliation status is authoritative
            for did in (doc.get("applied_deal_ids") or [])[-500:]:
                self._applied_deal_ids.append(did)
        # round 7 item 5 — account-level state (risk doc, stop risk, open
        # count) initializes ONCE per account, not per symbol runner
        await _restore_account_state(db, self.account_id)
        # round 5 item 5 — reconstruct DETAILED open-position state so a
        # restarted process can still run max-holding / spread-shock exits
        # and attribute exit slippage correctly.
        open_trades = await db.trades.find(
            {"account_id": self.account_id, "scope": "scalp_fast",
             "status": "open"}).to_list(50)
        self.risk_state.open_scalps = len(open_trades)
        for tr in open_trades:
            tid = str(tr["_id"])
            if tid in self.live_trades:
                continue
            pend = tr.get("pending_modification") or {}
            info = {
                "state": "CLOSE_REQUESTED" if pend.get("type") == "FULL_CLOSE" else "OPEN",
                "opened_ms": _iso_to_ms(tr.get("opened_at")) or now_ms(),
                "direction": tr.get("action"),
                "lot": float(tr.get("lot_size") or 0),
                "entry_px": tr.get("entry_price"),
                "requested_entry": tr.get("requested_price") or tr.get("intended_entry_price"),
                "actual_entry": tr.get("entry_price"),
                "entry_slippage_pips": tr.get("slippage_pips"),
                "stop_px": tr.get("stop_loss"), "target_px": tr.get("take_profit"),
                "decision_id": tr.get("scalp_decision_id", ""),
                "est_cost_usd": 0.0,
            }
            if info["state"] == "CLOSE_REQUESTED":
                info["close_requested_ms"] = _iso_to_ms(pend.get("requested_at")) or now_ms()
            if tr.get("scalp_decision_id"):
                dec = await db.scalp_decisions.find_one(
                    {"decision_id": tr["scalp_decision_id"]},
                    {"cost_pips": 1, "lot": 1})
                if dec:
                    from pip_utils import pip_value_usd_per_lot
                    pv = pip_value_usd_per_lot(self.symbol, None) or 10.0
                    info["est_cost_usd"] = round(
                        float(dec.get("cost_pips") or 0) * pv
                        * float(dec.get("lot") or 0), 2)
            self.live_trades[tid] = info
        self._risk_restored = True

    # ---------------- tick recording (Step 2, off hot path) ----------------

    def _maybe_flush_ticks(self, db):
        nm = now_ms()
        if (len(self._tick_buffer) >= TICK_FLUSH_N
                or (self._tick_buffer and nm - self._last_flush_ms >= TICK_FLUSH_MS)):
            batch, self._tick_buffer = self._tick_buffer, []
            self._last_flush_ms = nm
            _bg(lambda b=batch: db.scalp_ticks.insert_one({
                "user_id": self.user_id, "account_id": self.account_id,
                "symbol": self.symbol, "n": len(b),
                "first_ms": b[0]["tm"], "last_ms": b[-1]["tm"],
                "stored_at": datetime.now(timezone.utc).isoformat(),
                "ticks": b}), "tick_batch")

    def fill_stats(self) -> dict:
        """Empirical fill probability with an optimistic Beta(8,1) prior so a
        cold start never blocks; live rejections pull it down fast."""
        p = (self.exec_fills + 8.0) / (self.exec_attempts + 9.0)
        return {"attempts": self.exec_attempts, "fills": self.exec_fills,
                "p_fill": round(p, 3)}

    def status(self) -> dict:
        feats = snapshot(self.state)
        return {
            "account_id": self.account_id, "symbol": self.symbol,
            "enabled": self.enabled, "mode": self.mode,
            "model_key": self.model_key(),
            "risk_restored": self._risk_restored,
            "health": self.health,
            "permissions": permissions.get_cached(self.user_id, self.symbol),
            "counters": self.counters,
            "open_sims": len(self.open_sims),
            "open_live": len(self.live_trades),
            "fill_stats": self.fill_stats(),
            "awaiting_financials": len(self._closed_awaiting_financials),
            "live_states": {tid: i.get("state") for tid, i in self.live_trades.items()},
            "equity": self.equity,
            "spread_pips": self.state.spread_pips(),
            "quote_age_ms": self.state.quote_age_ms(),
            "features": ({k: round(v, 3) for k, v in feats.items()
                          if not k.startswith("_")} if feats else None),
            "risk": {
                "consecutive_losses": self.risk_state.consecutive_losses,
                "daily_loss_usd": round(self.risk_state.daily_loss_usd, 2),
                "daily_cost_usd": round(self.risk_state.daily_cost_usd, 2),
                "open_scalps": self.risk_state.open_scalps,
            },
            "account_risk": {
                "consecutive_losses": self.account_risk.consecutive_losses,
                "daily_loss_usd": round(self.account_risk.daily_loss_usd, 2),
                "daily_cost_usd": round(self.account_risk.daily_cost_usd, 2),
                "open_scalps": self.account_risk.open_scalps,
            },
            "last_decision": ({k: v for k, v in self.last_decision.items()
                               if k != "features"} if self.last_decision else None),
        }


def _oid(s):
    from bson import ObjectId
    try:
        return ObjectId(s)
    except Exception:
        return s


def _iso_to_ms(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        return int(datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
                   .timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def _bg(factory, desc: str = ""):
    """Schedule a persistence op. `factory` is a CALLABLE returning a fresh
    coroutine — never a coroutine object — so retries can re-create it and
    nothing leaks when no loop is running (round 4 item 1)."""
    global _audit_pending
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    _audit_pending += 1
    loop.create_task(_run_operation(factory, desc))
    return True


async def _run_operation(factory, desc: str = ""):
    """One REAL retry (fresh coroutine), then durable file dead-letter."""
    global _audit_pending, _audit_failures
    try:
        for attempt in range(2):
            try:
                await factory()
                return
            except Exception as e:  # noqa: BLE001
                if attempt == 0:
                    await asyncio.sleep(0.5)
                else:
                    _audit_failures += 1
                    _dead_letter(desc, str(e))
    finally:
        _audit_pending = max(0, _audit_pending - 1)


DEAD_LETTER_PATH = os.environ.get(
    "SCALP_DEAD_LETTER_PATH",
    str(Path(__file__).resolve().parent.parent / "scalp_dead_letter.jsonl"))


def _dead_letter(desc: str, error: str):
    """Round 6 item 10 / round 7 item 10 — synchronous durable append FIRST
    (local JSONL), then best-effort async replication to the Mongo outbox.
    The file write completes before this function returns, so a failing or
    shutting-down process still leaves a durable record."""
    rec = {"ts": datetime.now(timezone.utc).isoformat(),
           "op": desc, "error": error, "pid": _owner_pid}
    try:
        import json
        p = Path(DEAD_LETTER_PATH)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            f.write(json.dumps(rec) + "\n")
            f.flush()
    except OSError:
        logger.error("scalp dead-letter write failed: %s / %s", desc, error)
    try:
        from database import get_db
        loop = asyncio.get_running_loop()
        loop.create_task(get_db().scalp_dead_letter.insert_one(dict(rec)))
    except Exception:  # noqa: BLE001 — replication is best-effort
        pass


def audit_backlog() -> dict:
    # counters are PROCESS-LOCAL (round 5 item 3): pid identifies the worker
    # that owns the runners; multi-worker deployments need sticky routing.
    return {"pending": _audit_pending, "failures": _audit_failures,
            "halted": _audit_pending >= AUDIT_BACKLOG_HALT,
            "pid": _owner_pid,
            "service_block": _service_block_reason,
            "dead_letter_path": DEAD_LETTER_PATH}


def get_runner(account_id: str, user_id: str, symbol: str) -> ScalpRunner | None:
    if approved(symbol) is None:
        return None
    key = f"{account_id}:{symbol.upper()}"
    r = _runners.get(key)
    if r is None:
        r = ScalpRunner(account_id, user_id, symbol.upper())
        _runners[key] = r
    return r


def runners_for_account(account_id: str) -> list:
    return [r for k, r in _runners.items() if k.startswith(f"{account_id}:")]


async def apply_broker_deal(db, account_id: str, trade: dict, *, deal_id,
                            lots: float, profit: float, commission: float,
                            swap: float, price, partial: bool,
                            remaining_lots: float | None = None) -> dict:
    """Round 7 critical item — the ONE way a broker deal reaches a scalp
    runner. Returns {"applied": bool, "reason": str|None}; callers may mark
    the broker deal reconciliation-complete ONLY when applied is True.
    Constructs and restores the runner if it does not exist yet."""
    from pip_utils import base_symbol
    symbol = base_symbol(trade.get("symbol") or "")
    if not await ensure_account_lease(db, account_id):
        return {"applied": False, "reason": "account_owned_by_other_worker"}
    r = get_runner(account_id, str(trade.get("user_id") or ""), symbol)
    if r is None:
        return {"applied": False, "reason": "runner_unavailable"}
    if not r._risk_restored:
        try:
            await r.restore_risk(db)
        except Exception as e:  # noqa: BLE001
            return {"applied": False, "reason": f"restore_failed: {e}"}
    # Round 10 item 2 — durable ledger-first state machine. The financial
    # event is built from immutable broker-deal facts and persisted as
    # PENDING before any risk mutation. On crash-recovery this upsert
    # re-creates a missing ledger event even when the runner's dedup cache
    # (applied_deal_ids restored from risk state) skips the risk mutation.
    from scalp.deals import build_financial_event
    ev = build_financial_event(
        account_id=account_id, symbol=symbol, trade_id=str(trade["_id"]),
        deal_id=deal_id,
        event_type="partial_close" if partial else "full_close",
        profit=profit, commission=commission, swap=swap,
        remaining_lots=(remaining_lots if partial else None),
        lease_epoch=_lease_epoch.get(account_id, 0))
    ev_key = {"account_id": ev["account_id"], "deal_id": ev["deal_id"],
              "event_type": ev["event_type"]}
    try:
        await db.scalp_financial_events.update_one(
            ev_key,
            {"$setOnInsert": {**ev, "status": "pending",
                              "risk_applied": False}}, upsert=True)
    except Exception as e:  # noqa: BLE001
        return {"applied": False, "reason": f"ledger_write_failed: {e}"}
    try:
        if partial:
            r.on_partial_close(
                trade_id=str(trade["_id"]), closed_lots=float(lots or 0),
                remaining_lots=(float(remaining_lots)
                                if remaining_lots is not None
                                else float(trade.get("lot_size") or 0)),
                pnl=float(profit or 0), commission=float(commission or 0),
                swap=float(swap or 0),
                exit_price=float(price) if price else None,
                deal_id=str(deal_id), db=db)
        else:
            r.on_trade_closed(
                trade_id=str(trade["_id"]), pnl=float(profit or 0),
                commission=float(commission or 0), swap=float(swap or 0),
                exit_price=float(price) if price else None,
                deal_id=str(deal_id), close_reason=trade.get("close_reason"),
                source="broker_deal", db=db)
        # item 6 — risk state + applied deal id are DURABLE before the
        # caller may flip the broker deal to complete
        await r.persist_risk_now(db)
        # ledger event flips to APPLIED only after the fenced risk persist
        await db.scalp_financial_events.update_one(
            ev_key,
            {"$set": {"status": "applied", "risk_applied": True,
                      "applied_at": datetime.now(timezone.utc).isoformat()}})
    except Exception as e:  # noqa: BLE001
        return {"applied": False, "reason": f"apply_failed: {e}"}
    return {"applied": True, "reason": None}


async def verify_durable_invariants(db) -> dict:
    """Round 9 item 3 — DATABASE-side invariants that cannot fail open when
    no runner is loaded: any account with open scalp trades in the DB but no
    restored runner stays BLOCKED until restoration completes; open scalps
    without a protective stop block their account too; a restored runner's
    live position set must equal the DB's open scalp set (broker == DB is
    enforced separately on every heartbeat by trade_reconciler)."""
    blocked = []
    open_trades = await db.trades.find(
        {"scope": "scalp_fast", "status": "open"}).to_list(200)
    by_account: dict = {}
    for tr in open_trades:
        by_account.setdefault(str(tr.get("account_id")), []).append(tr)
    for account_id, trs in by_account.items():
        runners = runners_for_account(account_id)
        restored = any(r._risk_restored for r in runners)
        no_stop = [t for t in trs if not float(t.get("stop_loss") or 0)]
        position_mismatch = False
        if restored:
            db_ids = {str(t["_id"]) for t in trs}
            runner_ids = {tid for r in runners
                          for tid, lt in r.live_trades.items()
                          if lt.get("state") == "OPEN"}
            position_mismatch = db_ids != runner_ids
        if not restored or no_stop or position_mismatch:
            _invariant_block.add(account_id)
            blocked.append({"account_id": account_id,
                            "open_in_db": len(trs),
                            "runner_restored": restored,
                            "position_mismatch": position_mismatch,
                            "unstopped": len(no_stop)})
        elif account_id in _invariant_block:
            # clean durable state — re-run the in-memory checks, which
            # clear the block themselves on a clean pass
            verify_account_invariants(account_id)
    if blocked:
        logger.error("durable invariant violations: %s", blocked)
    # Round 10 item 4 — financial-ledger invariants (DB-side, runner-free):
    #   BLOCKING: every scalp broker deal reconciled in the last 24h must
    #   have its ledger event (a reconciled deal with no event means the
    #   ledger silently lost a financial fact).
    #   WARNING: per-account sum of today's applied negative net-P&L events
    #   should equal the persisted _ACCOUNT daily_loss_usd (report-path
    #   closes without a deal_id can legitimately diverge, so mismatches
    #   are surfaced for review instead of hard-blocking).
    fin_blocked, fin_warnings = [], []
    day_ago = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    async for deal in db.broker_deals.find(
            {"financial_reconciliation_status": "complete",
             "financial_reconciled_at": {"$gte": day_ago},
             "reconciliation_note": {"$ne": "not_scalp_scope"}}).limit(500):
        n = await db.scalp_financial_events.count_documents(
            {"account_id": deal["account_id"], "deal_id": str(deal["deal_id"])})
        if n == 0:
            acct = str(deal["account_id"])
            _invariant_block.add(acct)
            fin_blocked.append({"account_id": acct,
                                "deal_id": str(deal["deal_id"]),
                                "issue": "reconciled_deal_missing_ledger_event"})
    day_start = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00")
    async for rs in db.scalp_risk_state.find({"symbol": "_ACCOUNT"}).limit(200):
        acct = str(rs["account_id"])
        loss_sum = 0.0
        async for ev in db.scalp_financial_events.find(
                {"account_id": acct, "risk_applied": True,
                 "at": {"$gte": day_start}}).limit(500):
            net = float(ev.get("net_pnl") or 0)
            if net < 0:
                loss_sum += -net
        persisted = float(rs.get("daily_loss_usd") or 0)
        tol = max(0.02, 0.001 * max(loss_sum, persisted))
        if abs(loss_sum - persisted) > tol:
            fin_warnings.append({
                "account_id": acct,
                "issue": "daily_loss_ledger_mismatch",
                "ledger_loss_sum": round(loss_sum, 2),
                "persisted_daily_loss": round(persisted, 2)})
    if fin_blocked:
        logger.error("financial ledger invariant violations: %s", fin_blocked)
    if fin_warnings:
        logger.warning("financial ledger warnings: %s", fin_warnings)
    return {"blocked": blocked, "financial_blocked": fin_blocked,
            "financial_warnings": fin_warnings}


async def recover_pending_deals(db, older_than_sec: int = 60,
                                limit: int = 100) -> dict:
    """Round 6/7 — crash-recovery sweep. A deal flips to 'complete' ONLY
    after a runner confirmed the financial application; deals that are not
    scalp targets complete with an explicit note; failures stay pending
    with reconciliation_error + attempt count."""
    cutoff = (datetime.now(timezone.utc)
              - timedelta(seconds=older_than_sec)).isoformat()
    cur = db.broker_deals.find(
        {"financial_reconciliation_status": "pending",
         "received_at": {"$lt": cutoff}}).limit(limit)
    recovered = closed_out = kept_pending = 0
    async for deal in cur:
        key = {"deal_id": deal["deal_id"], "account_id": deal["account_id"]}
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            trade = await db.trades.find_one(
                {"account_id": deal["account_id"],
                 "mt5_ticket": deal.get("mt5_ticket")})
            if not trade or trade.get("scope") != "scalp_fast":
                if not trade:
                    # round 8 item 7 — a missing trade can be a RACE (deal
                    # arrived before trade adoption): grace-retry, then
                    # escalate to manual review; never silently complete.
                    attempts = int(deal.get("reconciliation_attempts") or 0)
                    if attempts < 5:
                        await db.broker_deals.update_one(key, {
                            "$set": {"reconciliation_error": "no_matching_trade_yet"},
                            "$inc": {"reconciliation_attempts": 1}})
                        kept_pending += 1
                    else:
                        await db.broker_deals.update_one(key, {"$set": {
                            "financial_reconciliation_status":
                                "manual_reconciliation_required",
                            "reconciliation_note": "no_matching_trade",
                            "escalated_at": now_iso}})
                        kept_pending += 1
                    continue
                # trade exists but is not a scalp target — definitive
                await db.broker_deals.update_one(key, {"$set": {
                    "financial_reconciliation_status": "complete",
                    "financial_reconciled_at": now_iso,
                    "financial_reconciled_by": "recovery_job",
                    "reconciliation_note": "not_scalp_scope"}})
                closed_out += 1
                continue
            res = await apply_broker_deal(
                db, deal["account_id"], trade,
                deal_id=deal["deal_id"], lots=deal.get("lots") or 0,
                profit=deal.get("profit") or 0,
                commission=deal.get("commission") or 0,
                swap=deal.get("swap") or 0, price=deal.get("price"),
                partial=(trade.get("status") == "open"))
            if res["applied"]:
                await db.broker_deals.update_one(key, {"$set": {
                    "financial_reconciliation_status": "complete",
                    "financial_reconciled_at": now_iso,
                    "financial_reconciled_by": "recovery_job"}})
                recovered += 1
                closed_out += 1
            else:
                await db.broker_deals.update_one(key, {
                    "$set": {"reconciliation_error": res["reason"]},
                    "$inc": {"reconciliation_attempts": 1}})
                kept_pending += 1
        except Exception as e:  # noqa: BLE001 — keep sweeping other deals
            logger.warning("scalp deal recovery failed deal=%s: %s",
                           deal.get("deal_id"), e)
            kept_pending += 1
    return {"recovered": recovered, "marked_complete": closed_out,
            "kept_pending": kept_pending}


async def apply_config(db, account: dict, symbol: str, enabled: bool, mode: str,
                       commission_usd_per_lot_side: float = 0.0):
    r = get_runner(str(account["_id"]), account["user_id"], symbol)
    if r is None:
        return None
    r.enabled = enabled
    r.mode = mode
    r.account = account
    r.broker = str(account.get("broker") or "")
    r.account_type = str(account.get("account_type") or "")
    r.commission_usd_per_lot_side = float(commission_usd_per_lot_side or 0.0)
    await r.restore_risk(db)
    await scalp_model.load_persisted(db, r.model_key())
    await db.scalp_configs.update_one(
        {"account_id": str(account["_id"]), "symbol": symbol.upper()},
        {"$set": {"user_id": account["user_id"], "enabled": enabled, "mode": mode,
                  "commission_usd_per_lot_side": r.commission_usd_per_lot_side,
                  "updated_at": datetime.now(timezone.utc).isoformat()}},
        upsert=True)
    return r
