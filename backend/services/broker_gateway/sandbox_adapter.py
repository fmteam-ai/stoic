"""Sandbox broker — a fully functional simulated broker-hosted PAMM engine.

State lives in its OWN collections (sandbox_broker_*) so it is a genuinely
independent source of truth for the reconciliation engine. NAV performs a
small deterministic random walk on every read to exercise drift handling.
A real broker adapter later implements the same BrokerAdapter interface.
"""
import random
import uuid
from datetime import datetime, timezone

from services.broker_gateway.broker_adapter import BrokerAdapter


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SandboxAdapter(BrokerAdapter):

    async def ensure_program(self, name: str, currency: str = "USD",
                             initial_nav: float = 100000.0) -> dict:
        """Sandbox-only helper: provision a PAMM program on the 'broker'."""
        program_id = f"sbx_{uuid.uuid4().hex[:10]}"
        doc = {"program_id": program_id, "partner_id": self.partner_id,
               "name": name, "currency": currency, "status": "active",
               "trading": "enabled", "nav": initial_nav,
               "master_login": f"9{random.randint(1000000, 9999999)}",
               "created_at": _now()}
        await self.db.sandbox_broker_programs.insert_one(dict(doc))
        doc.pop("_id", None)
        return doc

    async def get_pamm_programs(self) -> list:
        return [p async for p in self.db.sandbox_broker_programs.find(
            {"partner_id": self.partner_id}, {"_id": 0})]

    async def _program(self, program_id: str) -> dict:
        p = await self.db.sandbox_broker_programs.find_one(
            {"program_id": program_id}, {"_id": 0})
        if not p:
            raise ValueError(f"broker: unknown program {program_id}")
        return p

    async def create_investor(self, program_id: str, investor: dict) -> dict:
        await self._program(program_id)
        doc = {"investor_id": f"inv_{uuid.uuid4().hex[:10]}",
               "program_id": program_id, "partner_id": self.partner_id,
               "name": (investor.get("name") or "investor")[:80],
               "email": (investor.get("email") or "")[:120],
               "balance": 0.0, "status": "active", "created_at": _now()}
        await self.db.sandbox_broker_investors.insert_one(dict(doc))
        doc.pop("_id", None)
        return doc

    async def allocate(self, program_id: str, investor_id: str,
                       amount: float) -> dict:
        await self._program(program_id)
        inv = await self.db.sandbox_broker_investors.find_one(
            {"investor_id": investor_id, "program_id": program_id})
        if not inv:
            raise ValueError("broker: unknown investor")
        if amount <= 0:
            raise ValueError("broker: allocation must be positive")
        await self.db.sandbox_broker_investors.update_one(
            {"_id": inv["_id"]}, {"$inc": {"balance": float(amount)}})
        await self.db.sandbox_broker_programs.update_one(
            {"program_id": program_id}, {"$inc": {"nav": float(amount)}})
        alloc = {"allocation_id": f"alc_{uuid.uuid4().hex[:10]}",
                 "program_id": program_id, "investor_id": investor_id,
                 "amount": float(amount), "at": _now()}
        await self.db.sandbox_broker_allocations.insert_one(dict(alloc))
        alloc.pop("_id", None)
        return alloc

    async def get_nav(self, program_id: str) -> dict:
        p = await self._program(program_id)
        # deterministic-ish drift so NAV history and reconciliation have life
        drift = p["nav"] * random.uniform(-0.0015, 0.0018)
        nav = round(p["nav"] + drift, 2)
        await self.db.sandbox_broker_programs.update_one(
            {"program_id": program_id}, {"$set": {"nav": nav}})
        return {"program_id": program_id, "nav": nav,
                "currency": p["currency"], "at": _now()}

    async def get_master_account(self, program_id: str) -> dict:
        p = await self._program(program_id)
        investors = await self.db.sandbox_broker_investors.count_documents(
            {"program_id": program_id, "status": "active"})
        return {"program_id": program_id, "login": p["master_login"],
                "currency": p["currency"], "equity": p["nav"],
                "balance": p["nav"], "margin_level": 100.0,
                "investor_count": investors, "trading": p["trading"]}

    async def get_positions(self, program_id: str) -> list:
        await self._program(program_id)
        return [p async for p in self.db.sandbox_broker_positions.find(
            {"program_id": program_id}, {"_id": 0})]

    async def pause_trading(self, program_id: str) -> dict:
        await self._program(program_id)
        await self.db.sandbox_broker_programs.update_one(
            {"program_id": program_id}, {"$set": {"trading": "paused"}})
        return {"program_id": program_id, "trading": "paused"}

    async def resume_trading(self, program_id: str) -> dict:
        await self._program(program_id)
        await self.db.sandbox_broker_programs.update_one(
            {"program_id": program_id}, {"$set": {"trading": "enabled"}})
        return {"program_id": program_id, "trading": "enabled"}
