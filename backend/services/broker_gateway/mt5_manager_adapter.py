"""MT5 Manager API adapter (v55 §1) — real broker adapter targeting an
MT5 Manager HTTP gateway (the broker-side bridge that exposes the MT5
Manager API over REST). Credential-ready: point mt5_config at the
broker's gateway and run the certification suite. program_id maps to
the PAMM master login on the MT5 server. NO MT5-specific logic may
leak outside this file."""
from datetime import datetime, timezone

import httpx

from services.broker_gateway.broker_adapter import BrokerAdapter


def build_client(base_url: str, headers: dict | None = None,
                 timeout: float = 10.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=base_url.rstrip("/"),
                             headers=headers or {}, timeout=timeout)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Mt5ManagerAdapter(BrokerAdapter):

    def __init__(self, db, partner: dict):
        super().__init__(db, partner)
        self.cfg = partner.get("mt5_config") or {}
        for req in ("gateway_url", "manager_login", "server"):
            if not self.cfg.get(req):
                raise ValueError(f"mt5 adapter requires mt5_config.{req}")
        self._token: str | None = None

    def _password(self) -> str:
        enc = self.cfg.get("manager_password_enc")
        if not enc:
            raise ValueError(
                "mt5 adapter requires mt5_config.manager_password_enc")
        import secrets_vault
        return secrets_vault.decrypt(
            enc, associated_data=b"broker_manager_password")

    async def _authenticate(self, client: httpx.AsyncClient) -> None:
        r = await client.post("/auth/token", json={
            "login": self.cfg["manager_login"],
            "password": self._password(),
            "server": self.cfg["server"]})
        if r.status_code != 200:
            raise PermissionError(
                f"MT5 manager auth failed ({r.status_code})")
        self._token = (r.json() or {}).get("token")
        if not self._token:
            raise PermissionError("MT5 manager auth returned no token")

    async def _call(self, method: str, path: str,
                    json: dict | None = None):
        timeout = float(self.cfg.get("timeout_sec") or 10)
        async with build_client(self.cfg["gateway_url"],
                                timeout=timeout) as client:
            if not self._token:
                await self._authenticate(client)
            r = await client.request(
                method, path, json=json,
                headers={"Authorization": f"Bearer {self._token}"})
            if r.status_code == 401:  # token expired → one re-auth retry
                await self._authenticate(client)
                r = await client.request(
                    method, path, json=json,
                    headers={"Authorization": f"Bearer {self._token}"})
        if r.status_code == 404:
            raise ValueError(f"MT5 manager: unknown resource {path}")
        if r.status_code >= 400:
            raise ValueError(f"MT5 manager error {r.status_code} on {path}")
        return r.json()

    async def get_pamm_programs(self) -> list:
        masters = await self._call("GET", "/pamm/masters")
        return [{"program_id": str(m.get("login")),
                 "name": m.get("name"),
                 "currency": m.get("currency") or "USD",
                 "status": m.get("status") or "active",
                 "master_login": str(m.get("login")),
                 "nav": m.get("equity")} for m in masters or []]

    async def get_master_account(self, program_id: str) -> dict:
        m = await self._call("GET", f"/pamm/masters/{program_id}")
        return {"program_id": program_id, "login": str(m.get("login")),
                "currency": m.get("currency") or "USD",
                "equity": m.get("equity"), "balance": m.get("balance"),
                "margin_level": m.get("margin_level"),
                "investor_count": int(m.get("investor_count") or 0),
                "trading": ("enabled" if m.get("trading_enabled", True)
                            else "paused")}

    async def get_nav(self, program_id: str) -> dict:
        m = await self._call("GET", f"/pamm/masters/{program_id}")
        return {"program_id": program_id,
                "nav": float(m.get("equity") or 0),
                "currency": m.get("currency") or "USD", "at": _now()}

    async def get_positions(self, program_id: str) -> list:
        rows = await self._call("GET",
                                f"/pamm/masters/{program_id}/positions")
        return [{"position_id": str(p.get("ticket")),
                 "symbol": p.get("symbol"), "volume": p.get("volume"),
                 "side": p.get("type"), "open_price": p.get("open_price"),
                 "profit": p.get("profit")} for p in rows or []]

    async def create_investor(self, program_id: str, investor: dict) -> dict:
        r = await self._call("POST",
                             f"/pamm/masters/{program_id}/investors",
                             json={"name": investor.get("name"),
                                   "email": investor.get("email")})
        return {"investor_id": str(r.get("login") or r.get("investor_id")),
                "program_id": program_id, "name": investor.get("name"),
                "status": "active"}

    async def allocate(self, program_id: str, investor_id: str,
                       amount: float) -> dict:
        r = await self._call("POST",
                             f"/pamm/masters/{program_id}/deposits",
                             json={"investor_login": investor_id,
                                   "amount": float(amount)})
        return {"allocation_id": str(r.get("deal_id") or r.get("id")),
                "program_id": program_id, "investor_id": investor_id,
                "amount": float(amount), "at": _now()}

    async def pause_trading(self, program_id: str) -> dict:
        await self._call("POST",
                         f"/pamm/masters/{program_id}/trading/disable")
        return {"program_id": program_id, "trading": "paused"}

    async def resume_trading(self, program_id: str) -> dict:
        await self._call("POST",
                         f"/pamm/masters/{program_id}/trading/enable")
        return {"program_id": program_id, "trading": "enabled"}

    async def close_all_positions(self, program_id: str) -> dict:
        r = await self._call("POST",
                             f"/pamm/masters/{program_id}/close-all")
        return {"program_id": program_id,
                "closed": int((r or {}).get("closed") or 0)}
