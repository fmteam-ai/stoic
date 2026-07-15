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
import uuid
from datetime import datetime, timezone

from scalp import edge, gate, kill, permissions, setup
from scalp import model as scalp_model
from scalp.costs import dynamic_spread_limit
from scalp.features import snapshot
from scalp.forecast import make as make_forecast
from scalp.instruments import approved
from scalp.risk import DEFAULT_LIMITS, RiskState, check as risk_check
from scalp.state import ScalpState, TickEvent, now_ms

logger = logging.getLogger("scalp.engine")

EVAL_THROTTLE_MS = 1000
TICK_FLUSH_N = 200
TICK_FLUSH_MS = 10_000
RETRAIN_EVERY_RESOLUTIONS = 200
MAX_DRIFT_BEFORE_SUBMIT_FRAC = 0.5     # of stop distance, ABSOLUTE drift
MAX_BATCH_TRANSPORT_AGE_MS = 3000      # sent_at → arrival; older batches can't trade
AUDIT_BACKLOG_HALT = 500               # pending persist tasks that halt NEW entries

_runners: dict = {}
_audit_pending = 0
_audit_failures = 0


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
        # item 15 — account-level open-scalp cap across ALL runners
        account_open = sum(len(r.live_trades) for r in runners_for_account(self.account_id))
        if account_open >= self.risk_state.limits.max_open_positions:
            risk_res = {"ok": False, "lot": 0.0,
                        "reason": "account-level max open scalp positions"}
        else:
            risk_res = risk_check(self.risk_state, self.equity, fc.stop_pips,
                                  pip_val, self.cfg)
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
        if decision["direction"] == "BUY":
            sl, tp = entry - fc.stop_pips * pip, entry + fc.target_pips * pip
        else:
            sl, tp = entry + fc.stop_pips * pip, entry - fc.target_pips * pip
        from execution import engine_for_account
        account = self.account
        if not account:
            return
        engine = engine_for_account(account)
        submit_ms = now_ms()
        trade = await engine.execute(
            user_id=self.user_id, account=account,
            signal={"symbol": self.symbol, "action": decision["direction"],
                    "lot_size": risk_res["lot"],
                    "entry_price": round(entry, 5),
                    "stop_loss": round(sl, 5), "take_profit": round(tp, 5),
                    "origin": "auto", "scope": "scalp_fast",
                    "scalp_decision_id": decision["decision_id"]},
            cfg_account_id=self.account_id)
        if trade.get("blocked"):
            self.state.record_reject()
            _bg(lambda s={"verdict": "rejected", "reject_stage": "broker_blocked",
                          "block_reason": trade.get("reason")}: db.scalp_decisions.update_one(
                {"decision_id": decision["decision_id"]}, {"$set": s}),
                "decision_update")
            return
        tid = str(trade.get("id") or trade.get("_id") or "")
        self.risk_state.record_open()
        self._persist_risk(db)
        self.counters["live_trades"] += 1
        # estimated execution cost in USD for the daily cost budget (item 13)
        from pip_utils import pip_value_usd_per_lot
        pv = pip_value_usd_per_lot(self.symbol, None) or 10.0
        est_cost_usd = ((sp or 0) + 2 * fc.expected_slippage_pips) * pv * risk_res["lot"] \
            + self.commission_usd_per_lot_side * 2 * risk_res["lot"]
        self.live_trades[tid] = {
            "state": "OPEN",
            "opened_ms": now_ms(), "direction": decision["direction"],
            "entry_px": entry, "stop_px": sl, "target_px": tp,
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

    def on_trade_closed(self, trade_id: str, pnl: float,
                        commission: float = 0.0, swap: float = 0.0,
                        exit_price: float | None = None, db=None):
        """Broker-confirmed close (item 13/14): only NOW the position leaves
        the book and real execution cost hits the daily cost budget."""
        info = self.live_trades.pop(trade_id, None)
        est_cost = float(info.get("est_cost_usd", 0.0)) if info else 0.0
        real_cost = abs(commission) + abs(swap)
        total_cost = real_cost if real_cost > 0 else est_cost
        self.risk_state.record_close()
        self.risk_state.record_result(float(pnl or 0), total_cost)
        if db is not None:
            self._persist_risk(db)
            if info:
                # round 4 item 6 — canonical execution record with EXPLICIT
                # field semantics; consumers never guess what "pnl" means.
                outcome = {
                    "requested_entry_price": info.get("requested_entry"),
                    "actual_entry_price": info.get("actual_entry", info.get("entry_px")),
                    "actual_exit_price": exit_price,
                    "entry_slippage_pips": info.get("entry_slippage_pips"),
                    "gross_market_pnl_usd": float(pnl or 0),   # broker-reported, pre-commission
                    "commission_usd": float(commission or 0),
                    "swap_usd": float(swap or 0),
                    "net_pnl_usd": round(float(pnl or 0)
                                         - abs(commission) - abs(swap), 2),
                    "execution_cost_usd_used": round(total_cost, 2),
                    "cost_source": "broker" if real_cost > 0 else "estimated",
                }
                _bg(lambda o=outcome: db.scalp_decisions.update_one(
                    {"decision_id": info.get("decision_id", "")},
                    {"$set": {"dataset": "filled_live",
                              "execution_outcome": o,
                              "live_pnl_usd": o["net_pnl_usd"],
                              "live_cost_usd": o["execution_cost_usd_used"]}}),
                    "live_close")

    # ---------------- risk-state persistence (item 9) ----------------

    def _persist_risk(self, db):
        _bg(lambda: db.scalp_risk_state.update_one(
            {"account_id": self.account_id, "symbol": self.symbol},
            {"$set": {"user_id": self.user_id, **self.risk_state.to_doc(),
                      "saved_at": datetime.now(timezone.utc).isoformat()}},
            upsert=True), "risk_state")

    async def restore_risk(self, db):
        doc = await db.scalp_risk_state.find_one(
            {"account_id": self.account_id, "symbol": self.symbol})
        if doc:
            self.risk_state.load_doc(doc)
        # reconcile open live scalp positions from the trades collection
        open_trades = await db.trades.find(
            {"account_id": self.account_id, "scope": "scalp_fast",
             "status": "open"}).to_list(50)
        self.risk_state.open_scalps = len(open_trades)
        for tr in open_trades:
            tid = str(tr["_id"])
            if tid not in self.live_trades:
                pend = (tr.get("pending_modification") or {}).get("type")
                self.live_trades[tid] = {
                    "state": "CLOSE_REQUESTED" if pend == "FULL_CLOSE" else "OPEN",
                    "opened_ms": now_ms(),
                    "direction": tr.get("action"),
                    "entry_px": tr.get("entry_price"),
                    "stop_px": tr.get("stop_loss"), "target_px": tr.get("take_profit"),
                    "decision_id": tr.get("scalp_decision_id", ""),
                    "est_cost_usd": 0.0,
                }
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
            "last_decision": ({k: v for k, v in self.last_decision.items()
                               if k != "features"} if self.last_decision else None),
        }


def _oid(s):
    from bson import ObjectId
    try:
        return ObjectId(s)
    except Exception:
        return s


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


DEAD_LETTER_PATH = "/app/backend/scalp_dead_letter.jsonl"


def _dead_letter(desc: str, error: str):
    try:
        import json
        with open(DEAD_LETTER_PATH, "a") as f:
            f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(),
                                "op": desc, "error": error}) + "\n")
    except OSError:
        logger.error("scalp dead-letter write failed: %s / %s", desc, error)


def audit_backlog() -> dict:
    return {"pending": _audit_pending, "failures": _audit_failures,
            "halted": _audit_pending >= AUDIT_BACKLOG_HALT}


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
