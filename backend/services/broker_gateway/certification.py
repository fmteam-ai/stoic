"""Broker Adapter Certification Suite (review v54 §14).
One common contract every adapter must pass before handling managed money.
Produces a scored report persisted on the partner:
    Sandbox 100% → CERTIFIED · BrokerX 97% → NOT CERTIFIED."""
import logging
import time
import uuid
from datetime import datetime, timezone

from services.broker_gateway.auth import sign_payload, verify_webhook
from services.broker_gateway.broker_adapter import adapter_for

logger = logging.getLogger("pamm.certification")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _run_check(results: list, name: str, fn) -> bool:
    try:
        detail = await fn()
        results.append({"check": name, "status": "pass",
                        "detail": str(detail)[:160]})
        return True
    except NotImplementedError as e:
        results.append({"check": name, "status": "fail",
                        "detail": f"not implemented: {e}"[:160]})
    except Exception as e:
        results.append({"check": name, "status": "fail",
                        "detail": str(e)[:160]})
    return False


async def certify_adapter(db, partner: dict) -> dict:
    """Run the standard 13-check contract against one partner's adapter."""
    results: list = []
    adapter = None
    program = None
    temp_pid = None

    async def authentication():
        nonlocal adapter
        adapter = adapter_for(db, partner)
        await adapter.get_pamm_programs()
        return "adapter constructed + programs listed"
    await _run_check(results, "authentication", authentication)

    if adapter:
        async def program_identity():
            nonlocal program, temp_pid
            programs = await adapter.get_pamm_programs()
            if programs:
                program = programs[0]
            elif hasattr(adapter, "ensure_program"):
                program = await adapter.ensure_program(
                    f"cert-{uuid.uuid4().hex[:6]}")
                temp_pid = program["program_id"]
            else:
                raise ValueError("no program available to certify against")
            assert program.get("program_id"), "program_id missing"
            return f"program {program['program_id']}"
        await _run_check(results, "program_identity", program_identity)

    if program:
        pid = program["program_id"]

        async def master_identity():
            m = await adapter.get_master_account(pid)
            login = m.get("master_login") or m.get("login")
            assert login, "master login missing"
            return f"master {login}"

        async def nav():
            n = await adapter.get_nav(pid)
            assert isinstance(n.get("nav"), (int, float)), "nav not numeric"
            assert n.get("currency"), "currency missing"
            return f"nav={n['nav']} {n['currency']}"

        async def positions():
            p = await adapter.get_positions(pid)
            assert isinstance(p, list), "positions must be a list"
            return f"{len(p)} open"

        async def investor_and_allocation():
            inv = await adapter.create_investor(
                pid, {"name": "Cert Probe", "email": "cert@probe.local"})
            assert inv.get("investor_id"), "investor_id missing"
            alloc = await adapter.allocate(pid, inv["investor_id"], 100.0)
            assert alloc, "allocation returned nothing"
            return f"investor {inv['investor_id']} allocated 100.0"

        async def pause():
            r = await adapter.pause_trading(pid)
            return r

        async def resume():
            r = await adapter.resume_trading(pid)
            return r

        async def close_all():
            r = await adapter.close_all_positions(pid)
            assert "closed" in r, "close_all must report closed count"
            return r

        async def unknown_program_rejected():
            try:
                await adapter.get_nav("prg_does_not_exist_cert")
            except Exception:
                return "unknown program correctly rejected"
            raise AssertionError("unknown program silently accepted")

        for name, fn in (("master_identity", master_identity),
                         ("nav", nav), ("positions", positions),
                         ("investor_and_allocation", investor_and_allocation),
                         ("pause", pause), ("resume", resume),
                         ("close_all", close_all),
                         ("unknown_program_rejected",
                          unknown_program_rejected)):
            await _run_check(results, name, fn)

    async def stale_event_rejected():
        from services.broker_gateway.pamm_api import webhook_secret
        secret = webhook_secret(partner)
        stale_ts = str(time.time() - 3600)
        body = b'{"type":"NAVUpdated","event_id":"cert-stale"}'
        ok, reason = verify_webhook(secret, stale_ts,
                                    sign_payload(secret, stale_ts, body),
                                    body)
        assert not ok, "stale timestamp was accepted"
        return f"rejected: {reason}"
    await _run_check(results, "stale_event_rejected", stale_event_rejected)

    async def duplicate_event_idempotent():
        from modules.pamm.events import emit_event
        key = f"cert-dup-{uuid.uuid4().hex[:8]}"
        first = await emit_event(db, "NAVUpdated",
                                 {"cert_probe": True}, event_key=key)
        second = await emit_event(db, "NAVUpdated",
                                  {"cert_probe": True}, event_key=key)
        await db.pamm_events.delete_many({"event_key": key})
        assert first is not None and second is None, "dedupe failed"
        return "duplicate event_key ignored"
    await _run_check(results, "duplicate_event_idempotent",
                     duplicate_event_idempotent)

    async def reconciliation():
        stoic_prog = await db.pamm_programs.find_one(
            {"partner_id": partner["partner_id"]}, {"_id": 0})
        if not stoic_prog:
            return "SKIP"
        from services.broker_gateway.reconciliation import reconcile_program
        r = await reconcile_program(db, stoic_prog)
        assert isinstance(r, dict), "reconcile returned nothing"
        return "reconciled"
    try:
        detail = await reconciliation()
        if detail == "SKIP":
            results.append({"check": "reconciliation", "status": "skip",
                            "detail": "no STOIC program on this partner"})
        else:
            results.append({"check": "reconciliation", "status": "pass",
                            "detail": detail})
    except Exception as e:
        results.append({"check": "reconciliation", "status": "fail",
                        "detail": str(e)[:160]})

    # cleanup temp sandbox artifacts
    if temp_pid:
        for c in ("sandbox_broker_programs", "sandbox_broker_investors",
                  "sandbox_broker_allocations", "sandbox_broker_positions"):
            await getattr(db, c).delete_many({"program_id": temp_pid})

    passed = sum(1 for r in results if r["status"] == "pass")
    failed = sum(1 for r in results if r["status"] == "fail")
    score = round(passed / max(1, passed + failed) * 100, 1)
    certified = failed == 0 and passed >= 10
    report = {"score": score, "certified": certified, "passed": passed,
              "failed": failed,
              "skipped": sum(1 for r in results if r["status"] == "skip"),
              "at": _now(), "results": results}
    await db.broker_partners.update_one(
        {"partner_id": partner["partner_id"]},
        {"$set": {"certification": report}})
    logger.info("adapter certification %s: %s%% certified=%s",
                partner["partner_id"], score, certified)
    return {"partner_id": partner["partner_id"], **report}
