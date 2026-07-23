"""Live Trade Journal — AI-written, shareable post-mortem card for any
closed trade.

  POST   /api/journal/{trade_id}/card    — generate (or return cached) card
  GET    /api/journal/{trade_id}/card    — fetch existing card
  DELETE /api/journal/{trade_id}/card    — revoke the public share link
  GET    /api/public/journal/{share_id}  — unauthenticated masked card
"""
import html
import json
import logging
import os
import re
import secrets
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from auth import get_current_user
from database import get_db
from route_utils import parse_object_id
from security import rate_limit

logger = logging.getLogger("journal")

router = APIRouter(prefix="/journal", tags=["journal"])
public_router = APIRouter(prefix="/public", tags=["public-journal"])

_TAG_RE = re.compile(r"[^a-z0-9_]")
_HTML_RE = re.compile(r"<[^>]*>")

# public-narrative moderation — a shared card must not carry contact bait,
# links or abusive language
_MOD_PATTERNS = (
    ("contains_url", re.compile(r"https?://|www\.", re.I)),
    ("contains_email", re.compile(r"[\w.+-]+@[\w-]+\.[a-z]{2,}", re.I)),
    ("contains_phone", re.compile(r"\+?\d[\d\s().-]{8,}\d")),
    ("contains_profanity", re.compile(
        r"\b(fuck\w*|shit\w*|bitch\w*|cunt|nigg\w*|faggot|retard\w*)\b", re.I)),
    ("contains_solicitation", re.compile(
        r"\b(dm me|telegram me|whatsapp|join my|signal group|copy my trades|"
        r"guaranteed profit)\b", re.I)),
)
_CARD_TEXT_FIELDS = ("title", "summary", "what_went_right",
                     "what_went_wrong", "lesson")


def moderate_card(card: dict) -> list[str]:
    """Deterministic screen for public sharing — returns issue codes."""
    text = " ".join(str(card.get(f) or "") for f in _CARD_TEXT_FIELDS)
    text += " " + " ".join(card.get("hashtags") or [])
    return [code for code, rx in _MOD_PATTERNS if rx.search(text)]


def _clean(s: str, limit: int) -> str:
    return html.escape(_HTML_RE.sub("", str(s or "")), quote=False)[:limit].strip()


class JournalCardModel(BaseModel):
    """Strict schema for LLM output — enums, max lengths, sanitized tags,
    no HTML, unexpected fields rejected."""
    model_config = {"extra": "forbid"}
    title: str = Field(max_length=120)
    verdict: str
    summary: str = Field(max_length=600)
    what_went_right: str = Field(default="", max_length=400)
    what_went_wrong: str = Field(default="", max_length=400)
    lesson: str = Field(default="", max_length=300)
    grade: str
    hashtags: list[str] = Field(default_factory=list, max_length=5)

    @field_validator("verdict")
    @classmethod
    def _verdict(cls, v):
        v = str(v).upper().strip()
        if v not in ("WIN", "LOSS", "SCRATCH"):
            raise ValueError("bad verdict")
        return v

    @field_validator("grade")
    @classmethod
    def _grade(cls, v):
        v = str(v).upper().strip()[:1]
        if v not in ("A", "B", "C", "D", "F"):
            raise ValueError("bad grade")
        return v

    @field_validator("title", "summary", "what_went_right",
                     "what_went_wrong", "lesson", mode="before")
    @classmethod
    def _no_html(cls, v):
        return _clean(v, 600)

    @field_validator("hashtags", mode="before")
    @classmethod
    def _tags(cls, v):
        out = []
        for t in (v or [])[:5]:
            t = _TAG_RE.sub("", str(t).lower().lstrip("#"))[:24]
            if t:
                out.append(t)
        return out

_SYSTEM = """You are STOIC's trade-journal writer. Given one closed trade,
write an honest, punchy post-mortem card a trader would proudly share
publicly. Never invent facts not present in the data. Respond ONLY with
minified JSON, no code fences, exactly these keys:
{"title": "<max 8 words, no ticker spam>",
 "verdict": "WIN"|"LOSS"|"SCRATCH",
 "summary": "<2-3 sentences, plain language>",
 "what_went_right": "<1-2 sentences>",
 "what_went_wrong": "<1-2 sentences, empty string if nothing>",
 "lesson": "<one memorable takeaway sentence>",
 "grade": "A"|"B"|"C"|"D"|"F",
 "hashtags": ["<3-5 tags without #>"]}"""


async def _generate_card(trade: dict, include_reasoning: bool = False) -> dict:
    import os
    data = {
        "symbol": trade.get("symbol"), "action": trade.get("action"),
        "entry_price": trade.get("entry_price"),
        "exit_price": trade.get("exit_price"),
        "stop_loss": trade.get("stop_loss"),
        "take_profit": trade.get("take_profit"),
        "lot_size": trade.get("lot_size"), "pnl": trade.get("pnl"),
        "close_reason": trade.get("close_reason"),
        "opened_at": trade.get("opened_at") or trade.get("created_at"),
        "closed_at": trade.get("closed_at"),
    }
    # proprietary strategy reasoning stays PRIVATE unless the user opts in —
    # a public card must never leak internal signal logic
    if include_reasoning:
        data["ai_reasoning_at_entry"] = (trade.get("reasoning") or "")[:400]
    payload = json.dumps(data, default=str)
    try:
        from emergentintegrations.llm.chat import LlmChat, UserMessage
        chat = LlmChat(
            api_key=os.environ["EMERGENT_LLM_KEY"],
            session_id=f"journal-{trade.get('symbol', '?')}-{uuid.uuid4().hex[:8]}",
            system_message=_SYSTEM,
        ).with_model("anthropic", "claude-sonnet-4-5-20250929")
        raw = str(await chat.send_message(UserMessage(text=payload))).strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.lower().startswith("json"):
                raw = raw[4:].strip()
        card = JournalCardModel(**json.loads(raw)).model_dump()
        card["_llm_failed"] = False
        return card
    except Exception as e:  # noqa: BLE001
        logger.warning("journal LLM failed: %s", e)
        pnl = float(trade.get("pnl") or 0)
        verdict = "WIN" if pnl > 0 else "LOSS" if pnl < 0 else "SCRATCH"
        return {
            "title": f"{trade.get('symbol')} {str(trade.get('action', '')).upper()} — {verdict.lower()}",
            "verdict": verdict,
            "summary": (f"Closed {trade.get('symbol')} at "
                        f"{trade.get('exit_price')} for ${pnl:.2f} "
                        f"({trade.get('close_reason') or 'manual close'})."),
            "what_went_right": "", "what_went_wrong": "",
            "lesson": "AI narrative unavailable — numbers speak for themselves.",
            "grade": "B" if pnl > 0 else "D",
            "hashtags": ["stoic", "trading", str(trade.get("symbol", "")).lower()],
            "_llm_failed": True,
        }


def _trade_snapshot(trade: dict) -> dict:
    return {k: trade.get(k) for k in
            ("symbol", "action", "entry_price", "exit_price", "pnl",
             "close_reason", "opened_at", "closed_at", "pnl_source")}


def _payload(doc: dict) -> dict:
    return {"trade_id": doc["trade_id"], "share_id": doc.get("share_id"),
            "revoked": bool(doc.get("revoked")),
            "card": doc.get("card") or {}, "trade": doc.get("trade") or {},
            "ai_generated": not bool(doc.get("edited")),
            "edited": bool(doc.get("edited")),
            "created_at": doc.get("created_at")}


async def _owned_closed_trade(db, trade_id: str, user_id: str) -> dict:
    trade = await db.trades.find_one({"_id": parse_object_id(trade_id),
                                      "user_id": user_id})
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found")
    if trade.get("status") != "closed" or trade.get("exit_price") is None:
        raise HTTPException(status_code=400,
                            detail="Journal cards are for fully closed trades")
    return trade


@router.post("/{trade_id}/card")
async def create_card(trade_id: str, request: Request, force: bool = False,
                      include_reasoning: bool = False,
                      user=Depends(get_current_user)):
    """Create a PRIVATE journal card. Public sharing is a separate explicit
    action (POST /{trade_id}/share)."""
    db = get_db()
    trade = await _owned_closed_trade(db, trade_id, user["id"])
    existing = await db.trade_journal_cards.find_one(
        {"trade_id": trade_id, "user_id": user["id"]})
    if existing and not force:
        return _payload(existing)
    # regeneration burns LLM budget — cap per user per hour
    await rate_limit(db, "journal_generate", user["id"],
                     int(os.environ.get("JOURNAL_GEN_MAX_PER_HOUR", "15")),
                     3600,
                     "Journal generation limit reached — try again later.",
                     request=request)
    card = await _generate_card(trade, include_reasoning=include_reasoning)
    doc = {
        "trade_id": trade_id, "user_id": user["id"],
        # regeneration never resurrects sharing: an active share survives,
        # a revoked one stays revoked until explicitly re-shared
        "share_id": ((existing or {}).get("share_id")
                     if existing and not existing.get("revoked") else None),
        "revoked": bool((existing or {}).get("revoked")),
        "edited": False,
        "card": card, "trade": _trade_snapshot(trade),
        "created_at": datetime.now(timezone.utc),
    }
    await db.trade_journal_cards.update_one(
        {"trade_id": trade_id, "user_id": user["id"]},
        {"$set": doc}, upsert=True)
    return _payload(doc)


class CardEditIn(BaseModel):
    """User edits before publishing — same sanitation as the LLM schema."""
    title: str | None = Field(default=None, max_length=120)
    summary: str | None = Field(default=None, max_length=600)
    what_went_right: str | None = Field(default=None, max_length=400)
    what_went_wrong: str | None = Field(default=None, max_length=400)
    lesson: str | None = Field(default=None, max_length=300)

    @field_validator("title", "summary", "what_went_right",
                     "what_went_wrong", "lesson", mode="before")
    @classmethod
    def _no_html(cls, v):
        return None if v is None else _clean(v, 600)


@router.put("/{trade_id}/card")
async def edit_card(trade_id: str, payload: CardEditIn,
                    user=Depends(get_current_user)):
    """Let the owner refine the narrative before (or after) publishing."""
    db = get_db()
    doc = await db.trade_journal_cards.find_one(
        {"trade_id": trade_id, "user_id": user["id"]})
    if not doc:
        raise HTTPException(status_code=404, detail="No card yet")
    changes = {f"card.{k}": v for k, v in payload.model_dump().items()
               if v is not None}
    if not changes:
        raise HTTPException(status_code=422, detail="Nothing to update")
    changes["edited"] = True
    changes["edited_at"] = datetime.now(timezone.utc)
    await db.trade_journal_cards.update_one(
        {"trade_id": trade_id, "user_id": user["id"]}, {"$set": changes})
    doc = await db.trade_journal_cards.find_one(
        {"trade_id": trade_id, "user_id": user["id"]})
    return _payload(doc)


@router.post("/{trade_id}/share")
async def enable_share(trade_id: str, user=Depends(get_current_user)):
    """Explicitly enable (or rotate after revocation) the public share link.
    A previously revoked URL can NEVER become valid again — a fresh
    share_id is always issued after revocation."""
    db = get_db()
    doc = await db.trade_journal_cards.find_one(
        {"trade_id": trade_id, "user_id": user["id"]})
    if not doc:
        raise HTTPException(status_code=404, detail="Create the card first")
    if doc.get("share_id") and not doc.get("revoked"):
        return _payload(doc)                      # already publicly shared
    # public-narrative moderation gate — no links, contact bait or abuse
    issues = moderate_card(doc.get("card") or {})
    if issues:
        raise HTTPException(status_code=422, detail={
            "code": "moderation_failed",
            "message": "The card text cannot be shared publicly — edit it "
                       "first (PUT /journal/{trade_id}/card).",
            "issues": issues})
    new_id = secrets.token_urlsafe(12)
    await db.trade_journal_cards.update_one(
        {"trade_id": trade_id, "user_id": user["id"]},
        {"$set": {"share_id": new_id, "revoked": False}})
    doc.update({"share_id": new_id, "revoked": False})
    return _payload(doc)


@router.get("/{trade_id}/card")
async def get_card(trade_id: str, user=Depends(get_current_user)):
    db = get_db()
    doc = await db.trade_journal_cards.find_one(
        {"trade_id": trade_id, "user_id": user["id"]})
    if not doc:
        raise HTTPException(status_code=404, detail="No card yet")
    return _payload(doc)


@router.delete("/{trade_id}/card")
async def revoke_card(trade_id: str, user=Depends(get_current_user)):
    db = get_db()
    res = await db.trade_journal_cards.update_one(
        {"trade_id": trade_id, "user_id": user["id"]},
        {"$set": {"revoked": True}})
    return {"revoked": res.modified_count > 0}


@public_router.get("/journal/{share_id}")
async def public_journal(share_id: str):
    """Unauthenticated read-only journal card (no account identifiers)."""
    db = get_db()
    doc = await db.trade_journal_cards.find_one(
        {"share_id": share_id, "revoked": {"$ne": True}})
    if not doc:
        raise HTTPException(status_code=404,
                            detail="Share link not found or revoked")
    return {"share_id": share_id, "card": doc.get("card") or {},
            "trade": doc.get("trade") or {},
            "ai_generated": not bool(doc.get("edited")),
            "edited": bool(doc.get("edited")),
            "created_at": doc.get("created_at")}
