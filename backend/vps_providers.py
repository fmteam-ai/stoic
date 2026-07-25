"""VPS provider abstraction (correction: infra integration, Path A).

Every provider hides behind the same interface. Vultr is a REAL adapter
(activates when a Vultr API key is connected). Forex-focused providers
(ForexVPS, CNS, Beeks) require commercial partnerships and are exposed as
partner-gated stubs. `simulated` walks the full deployment state machine
locally so the pipeline is testable end-to-end without any provider account.
"""
import time
import uuid

import httpx

PROVIDERS = {
    "forexvps": {"label": "ForexVPS", "method": "partner",
                 "note": "API + MT4/MT5 templates via commercial partnership",
                 "available": False},
    "cns": {"label": "CNS", "method": "partner",
            "note": "Partner integration — contact us", "available": False},
    "beeks": {"label": "Beeks", "method": "partner",
              "note": "Partner integration — contact us", "available": False},
    "vultr": {"label": "Vultr", "method": "api_key",
              "note": "Self-service API — connect your Vultr API key",
              "available": True},
    "simulated": {"label": "Simulated Provider", "method": "none",
                  "note": "Walks the full deployment pipeline without a "
                          "real server — for evaluation",
                  "available": True},
}


class PartnerRequiredError(Exception):
    pass


class VpsProvider:
    name = "base"

    async def list_regions(self) -> list: ...
    async def list_plans(self) -> list: ...
    async def create_server(self, region: str, plan: str, os_id: str,
                            label: str, startup_script: str | None = None) -> dict: ...
    async def get_server(self, server_id: str) -> dict: ...
    async def reboot_server(self, server_id: str) -> dict: ...
    async def rebuild_server(self, server_id: str) -> dict: ...
    async def create_backup(self, server_id: str) -> dict: ...
    async def delete_server(self, server_id: str) -> dict: ...


class VultrProvider(VpsProvider):
    """Real adapter for https://api.vultr.com/v2 (API-key method)."""
    name = "vultr"
    BASE = "https://api.vultr.com/v2"

    def __init__(self, api_key: str):
        self.api_key = api_key

    def _headers(self):
        return {"Authorization": f"Bearer {self.api_key}"}

    async def _req(self, method: str, path: str, **kw) -> dict:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.request(method, f"{self.BASE}{path}",
                                headers=self._headers(), **kw)
            r.raise_for_status()
            return r.json() if r.content else {}

    async def list_regions(self):
        d = await self._req("GET", "/regions")
        return [{"id": x["id"], "city": x.get("city"),
                 "country": x.get("country")} for x in d.get("regions", [])]

    async def list_plans(self):
        d = await self._req("GET", "/plans?type=vc2")
        return [{"id": x["id"], "vcpu": x.get("vcpu_count"),
                 "ram_mb": x.get("ram"), "disk_gb": x.get("disk"),
                 "monthly_usd": x.get("monthly_cost")}
                for x in d.get("plans", [])]

    async def create_server(self, region, plan, os_id, label,
                            startup_script=None):
        body = {"region": region, "plan": plan, "os_id": int(os_id),
                "label": label}
        if startup_script:
            s = await self._req("POST", "/startup-scripts", json={
                "name": f"stoic-{label}", "type": "boot",
                "script": startup_script})
            body["script_id"] = s["startup_script"]["id"]
        d = await self._req("POST", "/instances", json=body)
        inst = d["instance"]
        return {"server_id": inst["id"], "status": inst.get("status"),
                "ip": inst.get("main_ip")}

    async def get_server(self, server_id):
        d = await self._req("GET", f"/instances/{server_id}")
        inst = d["instance"]
        return {"server_id": inst["id"], "status": inst.get("status"),
                "power": inst.get("power_status"), "ip": inst.get("main_ip"),
                "region": inst.get("region")}

    async def reboot_server(self, server_id):
        await self._req("POST", f"/instances/{server_id}/reboot")
        return {"ok": True}

    async def rebuild_server(self, server_id):
        await self._req("POST", f"/instances/{server_id}/reinstall")
        return {"ok": True}

    async def create_backup(self, server_id):
        d = await self._req("POST", "/snapshots",
                            json={"instance_id": server_id,
                                  "description": "stoic-backup"})
        return {"backup_id": d.get("snapshot", {}).get("id"), "ok": True}

    async def delete_server(self, server_id):
        await self._req("DELETE", f"/instances/{server_id}")
        return {"ok": True}


class SimulatedProvider(VpsProvider):
    """Walks the provisioning lifecycle on a timer — no real server."""
    name = "simulated"
    REGIONS = [{"id": "london", "city": "London", "country": "GB"},
               {"id": "amsterdam", "city": "Amsterdam", "country": "NL"},
               {"id": "frankfurt", "city": "Frankfurt", "country": "DE"},
               {"id": "newyork", "city": "New York", "country": "US"},
               {"id": "tokyo", "city": "Tokyo", "country": "JP"}]
    PLANS = [{"id": "2vcpu-4gb", "vcpu": 2, "ram_mb": 4096, "disk_gb": 80,
              "monthly_usd": 24},
             {"id": "4vcpu-8gb", "vcpu": 4, "ram_mb": 8192, "disk_gb": 160,
              "monthly_usd": 48},
             {"id": "8vcpu-16gb", "vcpu": 8, "ram_mb": 16384,
              "disk_gb": 320, "monthly_usd": 96}]

    async def list_regions(self):
        return self.REGIONS

    async def list_plans(self):
        return self.PLANS

    async def create_server(self, region, plan, os_id, label,
                            startup_script=None):
        return {"server_id": f"sim-{uuid.uuid4().hex[:10]}",
                "status": "pending", "ip": None,
                "created_ts": time.time()}

    async def get_server(self, server_id):
        return {"server_id": server_id, "status": "active",
                "ip": "203.0.113.10"}

    async def reboot_server(self, server_id):
        return {"ok": True}

    async def rebuild_server(self, server_id):
        return {"ok": True}

    async def create_backup(self, server_id):
        return {"backup_id": f"bak-{uuid.uuid4().hex[:8]}", "ok": True}

    async def delete_server(self, server_id):
        return {"ok": True}


class PartnerStubProvider(VpsProvider):
    def __init__(self, name: str):
        self.name = name

    def _blocked(self):
        raise PartnerRequiredError(
            f"{PROVIDERS[self.name]['label']} requires a commercial "
            f"partnership — this integration is partner-gated")

    async def list_regions(self):
        self._blocked()

    async def list_plans(self):
        self._blocked()

    async def create_server(self, *a, **k):
        self._blocked()

    async def get_server(self, *a, **k):
        self._blocked()

    async def reboot_server(self, *a, **k):
        self._blocked()

    async def rebuild_server(self, *a, **k):
        self._blocked()

    async def create_backup(self, *a, **k):
        self._blocked()

    async def delete_server(self, *a, **k):
        self._blocked()


def get_provider(name: str, api_key: str | None = None) -> VpsProvider:
    if name == "vultr":
        if not api_key:
            raise ValueError("Vultr requires a connected API key")
        return VultrProvider(api_key)
    if name == "simulated":
        return SimulatedProvider()
    if name in PROVIDERS:
        return PartnerStubProvider(name)
    raise ValueError(f"unknown provider '{name}'")
