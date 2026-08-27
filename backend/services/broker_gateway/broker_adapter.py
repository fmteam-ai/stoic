"""Uniform BrokerAdapter interface — every broker integration implements
this; the rest of STOIC never talks to a broker API directly."""
from abc import ABC, abstractmethod


class BrokerAdapter(ABC):
    """All monetary state returned here is the BROKER's truth — STOIC only
    mirrors it (pamm_* collections) and reconciles, never recomputes it."""

    def __init__(self, db, partner: dict):
        self.db = db
        self.partner = partner
        self.partner_id = partner["partner_id"]

    @abstractmethod
    async def get_pamm_programs(self) -> list: ...

    @abstractmethod
    async def create_investor(self, program_id: str, investor: dict) -> dict: ...

    @abstractmethod
    async def allocate(self, program_id: str, investor_id: str,
                       amount: float) -> dict: ...

    @abstractmethod
    async def get_nav(self, program_id: str) -> dict: ...

    @abstractmethod
    async def get_master_account(self, program_id: str) -> dict: ...

    @abstractmethod
    async def get_positions(self, program_id: str) -> list: ...

    @abstractmethod
    async def pause_trading(self, program_id: str) -> dict: ...

    @abstractmethod
    async def resume_trading(self, program_id: str) -> dict: ...


_REGISTRY: dict = {}


def register_adapter(kind: str, cls) -> None:
    _REGISTRY[kind] = cls


def adapter_for(db, partner: dict) -> BrokerAdapter:
    kind = partner.get("adapter", "sandbox")
    if kind not in _REGISTRY:
        if kind == "sandbox":
            from services.broker_gateway.sandbox_adapter import SandboxAdapter
            register_adapter("sandbox", SandboxAdapter)
        else:
            raise ValueError(f"no broker adapter registered for '{kind}'")
    return _REGISTRY[kind](db, partner)
