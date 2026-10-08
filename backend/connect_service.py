"""STOIC Connect (iter-154) — one call creates the account, issues the
pairing token and returns the single install command; one call reports the
whole connection pipeline (pairing → EA → identity → artifacts →
reconciliation → certificate) in plain language. Shared by the in-app
wizard AND the /v1 developer API."""
import os
from datetime import datetime, timedelta, timezone


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _age_s(iso) -> float | None:
    try:
        ts = datetime.fromisoformat(str(iso))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (_now_dt() - ts).total_seconds()
    except Exception:
        return None


def _base_url(request) -> str:
    """SEC-002 — never trust the raw Host header: PUBLIC_BASE_URL wins; otherwise the request host
    must match a configured CORS origin; then PUBLIC_BACKEND_URL / REACT_APP_BACKEND_URL; else the
    first https origin. N110-2 — the ONE source for the install one-liner, the claim response's
    server_url (→ STOIC-Server.txt) and the WebRequest host the user is told to allow-list."""
    env = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    if env:
        return env
    allowed = [o.strip().rstrip("/") for o in
               os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()]
    if request:
        req = str(request.base_url).rstrip("/")
        req_host = req.split("://", 1)[-1]
        for o in allowed:
            if o.split("://", 1)[-1] == req_host:
                return o
    backend = (os.environ.get("PUBLIC_BACKEND_URL") or os.environ.get("REACT_APP_BACKEND_URL") or "").rstrip("/")
    if backend:
        return backend
    https = [o for o in allowed if o.startswith("https://")]
    if https:
        return https[0]
    return str(request.base_url).rstrip("/") if request else ""


_INSTALLER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "STOIC-Installer.ps1")
_INSTALLER_HASH: dict = {}


def installer_sha256() -> str:
    """SHA-256 of the exact bytes GET /api/setup/installer.ps1 serves (cached per mtime) — pinned in the one-liner."""
    import hashlib
    st = os.stat(_INSTALLER)
    if _INSTALLER_HASH.get("mtime") != st.st_mtime_ns:
        with open(_INSTALLER, "rb") as f:
            _INSTALLER_HASH.update(mtime=st.st_mtime_ns, sha256=hashlib.sha256(f.read()).hexdigest().upper())
    return _INSTALLER_HASH["sha256"]


def install_command(base_url: str, token: str, sha256: str | None = None) -> str:
    """Easy-Connect hash pin: the one-liner downloads the installer, verifies its SHA-256 against the value
    pinned at issue time and refuses to run on a mismatch (PowerShell 5.1 and 7)."""
    sha256 = sha256 or installer_sha256()
    # N110-5 — TLS 1.2 BEFORE the first download (Server 2012 R2/2016 default .NET would fail the iwr)
    return (f'[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; '
            f'$r=iwr "{base_url}/api/setup/installer.ps1" -UseBasicParsing; '
            f'$h=(Get-FileHash -InputStream $r.RawContentStream -Algorithm SHA256).Hash; '
            f'if($h -ne "{sha256}"){{throw "STOIC installer hash mismatch ($h) - do not run"}}; '
            f'iex ([Text.Encoding]::UTF8.GetString($r.RawContentStream.ToArray()).TrimStart([char]0xFEFF)); '
            f'Install-Stoic -Token "{token}" -ServerUrl "{base_url}"')


async def start_connect(db, user: dict, payload: dict, request=None) -> dict:
    """Compose the EXISTING create-account + pairing-token flows (quota,
    dedupe and identity rules all apply) into one Stripe-like call."""
    from models import AccountCreate
    from routes.account_routes import create_account
    from routes.setup_routes import PairingTokenRequest, issue_pairing_token
    acc_payload = AccountCreate(
        label=str(payload.get("label") or "").strip() or "My MT5 Account",
        broker=str(payload.get("broker") or "").strip(),
        server=str(payload.get("server") or "").strip(),
        account_number=str(payload.get("account_number") or "").strip(),
        account_type=payload.get("account_type") or "standard",
        base_currency=payload.get("base_currency") or "USD",
        mode="live")
    account = await create_account(acc_payload, user=user)
    account_id = account["id"] if isinstance(account, dict) \
        else account.id
    pairing = await issue_pairing_token(
        PairingTokenRequest(account_id=str(account_id)), user=user)
    base = _base_url(request)
    return {"account_id": str(account_id),
            "pairing_token": pairing["token"],
            "pairing_expires_at": pairing["expires_at"],
            "install_command": install_command(base, pairing["token"]),
            "next": "Run the install command in PowerShell on the Windows "
                    "machine/VPS where MT5 runs, then poll the status "
                    "endpoint — STOIC handles the EA, certificates and "
                    "reconciliation from there."}


async def connect_status(db, account: dict) -> dict:
    acct_id = str(account["_id"])
    hb_age = _age_s(account.get("last_heartbeat"))
    since = (_now_dt() - timedelta(days=1)).isoformat()
    from soak_campaign import _account_invariants
    inv = await _account_invariants(db, acct_id, since)
    artifact = await db.artifact_digests.find_one(
        {"account_id": acct_id, "match": True})
    cert = await db.public_certificates.find_one(
        {"account_id": acct_id, "revoked": False,
         "expires_at": {"$gt": _now_dt().isoformat()}},
        sort=[("seq", -1)])
    steps = [
        {"key": "account_created", "label": "Account registered",
         "done": True, "detail": account.get("label")},
        {"key": "installer_paired",
         "label": "Installer paired (EA deployed)",
         "done": bool(account.get("installer_paired_at")),
         "detail": account.get("installer_paired_hostname")
         or "run the install command on your MT5 host"},
        {"key": "ea_online", "label": "EA heartbeat live",
         "done": hb_age is not None and hb_age < 180,
         "detail": (f"last heartbeat {int(hb_age)}s ago" if hb_age
                    is not None else "no heartbeat yet — attach the EA to "
                    "a chart with AutoTrading ON")},
        {"key": "identity_verified", "label": "Broker identity verified",
         "done": bool(account.get("verified_identity")),
         "detail": ((account.get("verified_identity") or {})
                    .get("broker_server")
                    or "verified automatically from the first full "
                    "heartbeat")},
        {"key": "artifacts_verified", "label": "Software integrity checked",
         "done": bool(artifact),
         "detail": ("EA binary digest matches the signed manifest"
                    if artifact else "reported by the installer after "
                    "pairing")},
        {"key": "reconciliation_clean", "label": "Positions reconciled",
         "done": not inv.get("unconfirmed_ghosts")
         and not inv.get("duplicate_executions"),
         "detail": (f"{inv.get('trades_24h') or 0} trade(s) checked — "
                    "no ghosts, no duplicates"
                    if not inv.get("unconfirmed_ghosts")
                    and not inv.get("duplicate_executions")
                    else "reconciliation anomalies — auto-heal running")},
        {"key": "certified", "label": "STOIC certificate issued",
         "done": bool(cert),
         "detail": (f"{cert['cert_id']} · {cert['tier']}" if cert else
                    "issue one from the Certification Center "
                    "(optional but recommended)")},
    ]
    required = steps[:6]
    done = sum(1 for s in required if s["done"])
    current = next((s for s in steps if not s["done"]), None)
    return {"account_id": acct_id, "label": account.get("label"),
            "state": "CONNECTED" if done == len(required) else "PENDING",
            "progress_pct": round(100 * done / len(required)),
            "current_step": current["label"] if current else "Fully connected",
            "steps": steps,
            "certificate_id": cert["cert_id"] if cert else None}
