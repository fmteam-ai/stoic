"""PAMM Investor Mirroring View — READ-ONLY by design.

An investor sees the master program's performance and their own mirrored
share. Nothing here mutates state; Execution Authority for PAMM_INVESTOR
accounts is LOCKED in the execution plane (execution_authority)."""
from execution_authority import account_execution_lock

PROGRAM_PUBLIC_FIELDS = (
    "program_id", "name", "currency", "status", "trading", "op_state",
    "emergency_stop", "investor_count", "aum", "last_nav", "manager_fee_pct",
    "created_at", "last_reconciled_at",
)


def _nav_at(navs_asc: list, at: str) -> float | None:
    """NAV snapshot in force at `at` (latest snapshot <= at, else the first
    snapshot after it). navs_asc is sorted by `at` ascending."""
    before = [n for n in navs_asc if (n.get("at") or "") <= at]
    if before:
        return before[-1].get("nav")
    return navs_asc[0].get("nav") if navs_asc else None


def investor_share(allocations: list, navs_asc: list,
                   current_nav: float | None) -> dict:
    """Pure: mirrored share of the master NAV.

    Each allocation buys a fraction = amount / NAV-at-allocation. The
    investor's estimated value is Σ fraction × current NAV; mirrored P&L is
    that value minus contributed capital. Always labelled ESTIMATED — the
    broker's allocation statement is the only authoritative figure."""
    contributed = 0.0
    fraction = 0.0
    priced = 0
    for a in allocations or []:
        amt = float(a.get("amount") or 0)
        contributed += amt
        nav_then = _nav_at(navs_asc, a.get("at") or "") or current_nav
        if nav_then and nav_then > 0:
            fraction += amt / float(nav_then)
            priced += 1
    est_value = round(fraction * float(current_nav), 2) \
        if current_nav and fraction else None
    share_pct = round(fraction * 100, 4) if fraction else None
    pnl = round(est_value - contributed, 2) if est_value is not None else None
    ret_pct = (round(pnl / contributed * 100, 3)
               if pnl is not None and contributed else None)
    return {"contributed": round(contributed, 2), "allocations": len(allocations or []),
            "priced_allocations": priced, "share_pct": share_pct,
            "estimated_value": est_value, "mirrored_pnl": pnl,
            "mirrored_return_pct": ret_pct, "estimated": True,
            "basis": "allocation ÷ broker NAV at allocation time"}


def redact_program(program: dict) -> dict:
    out = {k: program.get(k) for k in PROGRAM_PUBLIC_FIELDS}
    out["op_state"] = program.get("op_state") or "running"
    out["risk_breach"] = bool(program.get("risk_breach"))
    out["position_truth_status"] = ((program.get("position_truth") or {})
                                    .get("status"))
    return out


async def _linked_programs(db, user_id: str) -> dict:
    """program_id → {investor_ids:set, accounts:[...]} from BOTH sources:
    PAMM_INVESTOR accounts (pamm_program_id / pamm_broker_program_id) and
    approved marketplace join requests."""
    links: dict = {}

    def _slot(pid):
        return links.setdefault(pid, {"investor_ids": set(), "accounts": [],
                                      "sources": set()})

    async for a in db.accounts.find(
            {"user_id": user_id, "account_role": "PAMM_INVESTOR"},
            {"label": 1, "display_name": 1, "account_number": 1, "broker": 1,
             "balance": 1, "equity": 1, "last_heartbeat": 1,
             "pamm_program_id": 1, "pamm_broker_program_id": 1}):
        q = []
        if a.get("pamm_program_id"):
            q.append({"program_id": str(a["pamm_program_id"])})
            q.append({"broker_program_id": str(a["pamm_program_id"])})
        if a.get("pamm_broker_program_id"):
            q.append({"broker_program_id": str(a["pamm_broker_program_id"])})
        if not q:
            continue
        prog = await db.pamm_programs.find_one({"$or": q}, {"program_id": 1})
        if not prog:
            continue
        slot = _slot(prog["program_id"])
        slot["sources"].add("account")
        slot["accounts"].append({
            "account_id": str(a["_id"]),
            "label": a.get("display_name") or a.get("label"),
            "account_number": a.get("account_number"),
            "broker": a.get("broker"), "balance": a.get("balance"),
            "equity": a.get("equity"),
            "last_heartbeat": a.get("last_heartbeat")})

    async for r in db.pamm_join_requests.find(
            {"user_id": user_id, "status": "approved"},
            {"program_id": 1, "investor_id": 1}):
        slot = _slot(r["program_id"])
        slot["sources"].add("marketplace")
        if r.get("investor_id"):
            slot["investor_ids"].add(r["investor_id"])
    return links


async def _navs_asc(db, program_id: str, limit: int = 500) -> list:
    navs = [n async for n in db.pamm_nav_snapshots
            .find({"program_id": program_id}, {"_id": 0})
            .sort("at", -1).limit(limit)]
    navs.reverse()
    return navs


async def _my_allocations(db, program_id: str, investor_ids: set) -> list:
    if not investor_ids:
        return []
    return [a async for a in db.pamm_allocations.find(
        {"program_id": program_id, "investor_id": {"$in": sorted(investor_ids)}},
        {"_id": 0}).sort("at", 1)]


async def _program_row(db, program: dict, link: dict) -> dict:
    from modules.pamm.reports import performance_summary
    pid = program["program_id"]
    navs = await _navs_asc(db, pid)
    current_nav = (program.get("last_nav") or {}).get("nav") or program.get("aum")
    allocs = await _my_allocations(db, pid, link["investor_ids"])
    return {"program": redact_program(program),
            "performance": await performance_summary(db, pid),
            "share": investor_share(allocs, navs, current_nav),
            "linked_accounts": link["accounts"],
            "sources": sorted(link["sources"])}


async def investor_programs(db, user_id: str) -> list:
    links = await _linked_programs(db, user_id)
    if not links:
        return []
    rows = []
    async for p in db.pamm_programs.find(
            {"program_id": {"$in": sorted(links)}}, {"_id": 0}):
        rows.append(await _program_row(db, p, links[p["program_id"]]))
    rows.sort(key=lambda r: r["program"].get("name") or "")
    return rows


async def _master_trades(db, program: dict, limit: int = 25) -> dict:
    pid = program["program_id"]
    ors = [{"pamm_program_id": pid}]
    if program.get("master_account_id"):
        ors.append({"account_id": str(program["master_account_id"])})
    proj = {"_id": 0, "symbol": 1, "action": 1, "lot_size": 1, "pnl": 1,
            "entry_price": 1, "exit_price": 1, "opened_at": 1,
            "closed_at": 1, "status": 1}
    closed = [t async for t in db.trades.find(
        {"$or": ors, "status": "closed"}, proj).sort("closed_at", -1)
        .limit(limit)]
    open_count = await db.trades.count_documents({"$or": ors, "status": "open"})
    return {"recent_closed": closed, "open_positions": open_count}


async def investor_program_view(db, user: dict, program_id: str) -> dict | None:
    program = await db.pamm_programs.find_one({"program_id": program_id},
                                              {"_id": 0})
    if not program:
        return None
    links = await _linked_programs(db, user["id"])
    link = links.get(program_id)
    if link is None:
        if user.get("role") != "admin":
            return None
        link = {"investor_ids": set(), "accounts": [], "sources": ["admin"]}
    from modules.pamm.risk import trading_allowed
    allowed, reason = await trading_allowed(db, program)
    row = await _program_row(db, program, link)
    row["nav"] = await _navs_asc(db, program_id, limit=300)
    row["my_allocations"] = await _my_allocations(db, program_id,
                                                  link["investor_ids"])
    row["trading_allowed"] = allowed
    row["trading_block_reason"] = reason
    row["master_trades"] = await _master_trades(db, program)
    row["execution_authority"] = {
        "locked": True, "role": "PAMM_INVESTOR",
        "reason": account_execution_lock({"account_role": "PAMM_INVESTOR"})}
    return row
