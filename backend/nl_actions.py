"""Strict typed schemas for Risk Commander / trigger actions (audit r15 P1-01).

Every action the AI proposes is normalised through these models BEFORE it is
previewed or stored: bounded finite thresholds, enumerated conditions, targets
and risk levels, a small nested-action limit and no recursive triggers.
"""
import math
import re
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

MAX_ACTIONS = 8
MAX_NESTED_ACTIONS = 3
SYMBOL_RE = re.compile(r"^[A-Z0-9._-]{3,16}$")
RISK_LEVELS = ("low", "medium", "high", "extreme")


def _norm_target(v):
    v = str(v or "all").strip()
    if v.lower() in ("all", ""):
        return "all"
    if v.lower() == "high_risk":
        return "high_risk"
    up = v.upper()
    if not SYMBOL_RE.match(up):
        raise ValueError(f"invalid target {v!r}")
    return up


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: str = "all"

    @field_validator("target", mode="before")
    @classmethod
    def _t(cls, v):
        return _norm_target(v)


class _NoParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DisableBots(_Base):
    type: Literal["DISABLE_BOTS"]
    params: _NoParams = Field(default_factory=_NoParams)


class EnableBots(_Base):
    type: Literal["ENABLE_BOTS"]
    params: _NoParams = Field(default_factory=_NoParams)


class MoveStopsBreakeven(_Base):
    type: Literal["MOVE_STOPS_BREAKEVEN"]
    params: _NoParams = Field(default_factory=_NoParams)


class CloseAllTrades(_Base):
    type: Literal["CLOSE_ALL_TRADES"]
    params: _NoParams = Field(default_factory=_NoParams)


class PanicLock(_Base):
    type: Literal["PANIC_LOCK"]
    params: _NoParams = Field(default_factory=_NoParams)


class RiskParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    risk_level: Literal["low", "medium", "high", "extreme"]

    @field_validator("risk_level", mode="before")
    @classmethod
    def _lower(cls, v):
        return str(v).strip().lower()


class SetRiskLevel(_Base):
    type: Literal["SET_RISK_LEVEL"]
    params: RiskParams


LeafAction = Annotated[Union[DisableBots, EnableBots, MoveStopsBreakeven, CloseAllTrades,
                             PanicLock, SetRiskLevel], Field(discriminator="type")]


class TriggerParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = "BTCUSD"
    condition: Literal["drop", "rise", "move"] = "drop"
    threshold_pct: float = Field(gt=0.0, le=50.0)
    then: list[LeafAction] = Field(min_length=1, max_length=MAX_NESTED_ACTIONS)

    @field_validator("symbol", mode="before")
    @classmethod
    def _sym(cls, v):
        up = str(v or "BTCUSD").strip().upper()
        if not SYMBOL_RE.match(up):
            raise ValueError(f"invalid symbol {v!r}")
        return up

    @field_validator("condition", mode="before")
    @classmethod
    def _cond(cls, v):
        return str(v or "drop").strip().lower()

    @field_validator("threshold_pct", mode="before")
    @classmethod
    def _finite(cls, v):
        f = float(v)
        if not math.isfinite(f):
            raise ValueError("threshold_pct must be finite")
        return f


class SetConditionalTrigger(_Base):
    type: Literal["SET_CONDITIONAL_TRIGGER"]
    params: TriggerParams


Action = Annotated[Union[DisableBots, EnableBots, MoveStopsBreakeven, CloseAllTrades, PanicLock,
                         SetRiskLevel, SetConditionalTrigger], Field(discriminator="type")]


class ActionList(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actions: list[Action] = Field(min_length=1, max_length=MAX_ACTIONS)


def _pre(a: dict) -> dict:
    """Upper-case the discriminator and drop empty params so the union resolves."""
    out = dict(a or {})
    out["type"] = str(out.get("type") or "").upper()
    if out.get("params") in (None, {}):
        out.pop("params", None)
    if out.get("target") is None:
        out.pop("target", None)
    if out["type"] == "SET_CONDITIONAL_TRIGGER" and isinstance(out.get("params"), dict):
        then = out["params"].get("then")
        if isinstance(then, list):
            out["params"] = {**out["params"], "then": [_pre(t) if isinstance(t, dict) else t for t in then]}
    return out


def validate_actions(raw: list) -> list[dict]:
    """Return normalised action dicts or raise ValueError with a short reason."""
    try:
        parsed = ActionList(actions=[_pre(a) for a in (raw or [])])
    except (ValidationError, TypeError, AttributeError) as e:
        first = e.errors()[0] if isinstance(e, ValidationError) and e.errors() else {}
        loc = ".".join(str(x) for x in first.get("loc", []))
        raise ValueError(f"invalid action: {loc or 'actions'} — {first.get('msg', str(e))}") from e
    return [a.model_dump() for a in parsed.actions]
