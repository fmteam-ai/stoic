"""Generic REST broker adapter (v55 §1) — the first REAL wire-protocol
adapter. Talks to any broker exposing the standard STOIC PAMM REST
contract over HTTPS with bearer-key auth. Base URL, endpoint overrides
and credentials live in the partner document (rest_config) — NO
broker-specific logic may leak outside this file."""
import httpx

from services.broker_gateway.broker_adapter import BrokerAdapter

DEFAULT_ENDPOINTS = {
    "programs": "GET /programs",
    "master": "GET /programs/{pid}/master",
    "nav": "GET /programs/{pid}/nav",
    "positions": "GET /programs/{pid}/positions",
    "create_investor": "POST /programs/{pid}/investors",
    "allocate": "POST /programs/{pid}/allocations",
    "pause": "POST /programs/{pid}/pause",
    "resume": "POST /programs/{pid}/resume",
    "close_all": "POST /programs/{pid}/close-all",
}


def build_client(base_url: str, headers: dict, timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=base_url.rstrip("/"), headers=headers,
                             timeout=timeout)


class RestBrokerAdapter(BrokerAdapter):

    def __init__(self, db, partner: dict):
        super().__init__(db, partner)
        self.cfg = partner.get("rest_config") or {}
        if not self.cfg.get("base_url"):
            raise ValueError("rest adapter requires rest_config.base_url")
        self.endpoints = {**DEFAULT_ENDPOINTS,
                          **(self.cfg.get("endpoints") or {})}

    def _api_key(self) -> str:
        enc = self.cfg.get("api_key_enc")
        if not enc:
            raise ValueError("rest adapter requires rest_config.api_key_enc")
        import secrets_vault
        return secrets_vault.decrypt(enc, associated_data=b"broker_api_key")

    async def _call(self, name: str, pid: str | None = None,
                    json: dict | None = None):
        method, _, path = self.endpoints[name].partition(" ")
        if pid is not None:
            path = path.format(pid=pid)
        headers = {"Authorization": f"Bearer {self._api_key()}"}
        timeout = float(self.cfg.get("timeout_sec") or 10)
        async with build_client(self.cfg["base_url"], headers,
                                timeout) as client:
            r = await client.request(method, path, json=json)
        if r.status_code == 404:
            raise ValueError(f"broker: unknown resource ({name} {pid})")
        if r.status_code in (401, 403):
            raise PermissionError("broker rejected credentials")
        if r.status_code >= 400:
            raise ValueError(f"broker error {r.status_code}: {r.text[:160]}")
        return r.json()

    async def get_pamm_programs(self) -> list:
        return await self._call("programs")

    async def get_master_account(self, program_id: str) -> dict:
        return await self._call("master", program_id)

    async def get_nav(self, program_id: str) -> dict:
        return await self._call("nav", program_id)

    async def get_positions(self, program_id: str) -> list:
        return await self._call("positions", program_id)

    async def create_investor(self, program_id: str, investor: dict) -> dict:
        return await self._call("create_investor", program_id, json=investor)

    async def allocate(self, program_id: str, investor_id: str,
                       amount: float) -> dict:
        return await self._call("allocate", program_id,
                                json={"investor_id": investor_id,
                                      "amount": float(amount)})

    async def pause_trading(self, program_id: str) -> dict:
        return await self._call("pause", program_id, json={})

    async def resume_trading(self, program_id: str) -> dict:
        return await self._call("resume", program_id, json={})

    async def close_all_positions(self, program_id: str) -> dict:
        return await self._call("close_all", program_id, json={})
