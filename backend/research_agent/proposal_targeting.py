"""Research-proposal → bot-config target resolution.

A user can have multiple `bot_configs` (one per `(user_id, account_id)`,
plus an optional default profile where `account_id is None`). When a
research proposal is accepted, we must decide WHICH configs to update.

Targeting modes:
  • "matching" (default) — every config whose `symbols` set overlaps the
    proposal's `symbols` set. If the proposal has no symbols, falls back
    to "all".
  • "all"               — every bot_config the user owns.
  • "default"           — only the default profile (account_id is None).
  • "<account_id>"      — only the config bound to that specific account.

Every successful apply writes an audit row to the affected bot_config
(`last_research_proposal_id`, `last_research_applied_at`,
`last_research_target_mode`) so the user can later see exactly which bots
were touched and why.
"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Iterable

from bson import ObjectId


# ---------------------------------------------------------------------------
# Candidate listing — used by GET /proposals/{id}/targets to populate the
# UI dropdown.
# ---------------------------------------------------------------------------
async def list_candidate_bots(db, user_id: str, proposal_symbols: list[str] | None) -> list[dict]:
    """Return one row per bot_config the user owns, annotated with whether
    its symbols overlap the proposal's symbols.
    """
    proposal_set = set(s.upper() for s in (proposal_symbols or []) if s)
    out: list[dict] = []
    cursor = db.bot_configs.find({"user_id": user_id})
    docs = await cursor.to_list(length=100)

    # Pre-fetch account labels in one round-trip for nice UI labels.
    acct_ids = [d.get("account_id") for d in docs if d.get("account_id")]
    label_lookup: dict[str, str] = {}
    if acct_ids:
        try:
            obj_ids = [ObjectId(a) for a in acct_ids]
            accts = await db.accounts.find(
                {"_id": {"$in": obj_ids}, "user_id": user_id},
                {"label": 1, "broker": 1},
            ).to_list(length=100)
            for a in accts:
                label_lookup[str(a["_id"])] = a.get("label") or a.get("broker") or "—"
        except Exception:
            pass

    for d in docs:
        symbols = list(d.get("symbols") or [])
        symbol_set = set(s.upper() for s in symbols if s)
        account_id = d.get("account_id")
        is_default = account_id is None
        key = "default" if is_default else str(account_id)
        label = (
            "Default profile" if is_default
            else label_lookup.get(str(account_id), f"Account {str(account_id)[-6:]}")
        )
        if proposal_set:
            matches = bool(proposal_set & symbol_set) if symbol_set else False
        else:
            # Proposal has no symbol scope → matches everywhere.
            matches = True
        out.append({
            "key": key,
            "account_id": str(account_id) if account_id else None,
            "label": label,
            "symbols": symbols,
            "active": bool(d.get("active")),
            "risk_level": d.get("risk_level"),
            "matches_proposal_symbols": matches,
        })
    return out


# ---------------------------------------------------------------------------
# Resolver — turns a "target" mode string into the list of bot_configs
# that should actually be updated.
# ---------------------------------------------------------------------------
async def resolve_target_configs(db, user_id: str, proposal_symbols: list[str] | None,
                                 target: str) -> tuple[list[dict], str]:
    """Returns (matching_bot_config_docs, resolved_mode_description).

    resolved_mode_description is for the audit row and the UI toast
    (e.g. "matching:XAUUSD,BTCUSD", "all", "specific:abc123", "default").
    """
    target = (target or "matching").strip()
    proposal_set = set(s.upper() for s in (proposal_symbols or []) if s)

    if target == "all":
        cursor = db.bot_configs.find({"user_id": user_id})
        docs = await cursor.to_list(length=100)
        return docs, "all"

    if target == "default":
        cursor = db.bot_configs.find({
            "user_id": user_id,
            "$or": [{"account_id": None}, {"account_id": {"$exists": False}}],
        })
        docs = await cursor.to_list(length=10)
        return docs, "default"

    if target == "matching":
        cursor = db.bot_configs.find({"user_id": user_id})
        all_docs = await cursor.to_list(length=100)
        if not proposal_set:
            # No scope — same as "all"
            return all_docs, "matching:all_symbols"
        matched = [
            d for d in all_docs
            if proposal_set & set(s.upper() for s in (d.get("symbols") or []) if s)
        ]
        scope = ",".join(sorted(proposal_set))
        return matched, f"matching:{scope}"

    # target is a specific account_id string
    cursor = db.bot_configs.find({"user_id": user_id, "account_id": target})
    docs = await cursor.to_list(length=10)
    return docs, f"specific:{target}"


# ---------------------------------------------------------------------------
# Apply — single transactional step that updates every matched config
# atomically (per-doc) and stamps the audit row.
# ---------------------------------------------------------------------------
async def apply_to_bot_configs(db, configs: Iterable[dict], *, update_fields: dict,
                                source: str, source_id: str | None = None,
                                target_mode: str, auto: bool = False) -> list[dict]:
    """Apply `update_fields` to every config and stamp a unified audit row.

    Parameters:
      source       — what triggered the change. One of:
                       "research" / "research_auto_accept" / "nl_strategy" /
                       "risk_commander" / "ui_manual".
      source_id    — proposal ObjectId / strategy hash / NL prompt hash /
                     None for ad-hoc UI changes.
      target_mode  — resolved target string ("matching:XAUUSD", "all", …)
                     returned by `resolve_target_configs`.
      auto         — True when the change was applied automatically (no
                     human click). Powers the auto-accept badges in the UI.

    Returns one audit entry per touched config (for the API response).
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    audit: list[dict] = []

    for cfg in configs:
        stamped = {
            **update_fields,
            "updated_at": now_iso,
            "last_change_source": source,
            "last_change_source_id": source_id,
            "last_change_target_mode": target_mode,
            "last_change_applied_at": now_iso,
            "last_change_auto": bool(auto),
            # Backwards-compat: keep the iter-36 field names for the
            # research-only pathway so older audit consumers don't break.
            **({"last_research_proposal_id": source_id,
                "last_research_target_mode": target_mode,
                "last_research_applied_at": now_iso}
               if source == "research" and not auto else {}),
            **({"last_auto_accepted_at": now_iso}
               if auto else {}),
        }
        await db.bot_configs.update_one(
            {"_id": cfg["_id"]},
            {"$set": stamped},
        )
        audit.append({
            "config_id": str(cfg["_id"]),
            "account_id": (
                str(cfg["account_id"]) if cfg.get("account_id") else None
            ),
            "previous_symbols": list(cfg.get("symbols") or []),
            "previous_risk_level": cfg.get("risk_level"),
            "previous_strategy_style": cfg.get("strategy_style"),
            "is_default": cfg.get("account_id") is None,
        })
    return audit


# Backwards-compatible alias — iter-36 callers used this name. New code
# should prefer `apply_to_bot_configs` so the audit row clearly records
# the change source.
async def apply_proposal_to_configs(db, configs, *, update_fields, proposal_id,
                                    target_mode, auto=False):
    return await apply_to_bot_configs(
        db, configs,
        update_fields=update_fields,
        source="research_auto_accept" if auto else "research",
        source_id=proposal_id,
        target_mode=target_mode,
        auto=auto,
    )
