"""Promote an EXISTING registered user to admin (second approver for 4-eyes gates).

    docker compose exec -T backend python ops/promote_admin.py second.admin@example.com

Deliberately a host-side CLI (root shell on the server), not an API: granting admin
from the UI would let one admin manufacture its own second approver. The user must
already exist (register normally first); the promotion is written to admin_audit_log.
"""
import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
except Exception:  # noqa: BLE001
    pass
from secrets_loader import resolve_file_secrets  # noqa: E402
resolve_file_secrets()


async def main(email: str) -> int:
    from database import get_db
    db = get_db()
    user = await db.users.find_one({"email": email.lower()})
    if not user:
        print(f"REFUSED: no registered user {email!r} — register the account first, then re-run")
        return 1
    if user.get("role") == "admin":
        print(f"already admin: {email}")
        return 0
    now = datetime.now(timezone.utc)
    await db.users.update_one({"_id": user["_id"]}, {"$set": {"role": "admin", "promoted_at": now}})
    await db.admin_audit_log.insert_one({
        "at": now, "actor": "ops/promote_admin.py (host shell)", "action": "admin_promoted",
        "target_user_id": str(user["_id"]), "target_email": email.lower(),
    })
    print(f"promoted to admin: {email}  (admins now: {await db.users.count_documents({'role': 'admin'})})")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2 or "@" not in sys.argv[1]:
        print(__doc__)
        sys.exit(2)
    sys.exit(asyncio.run(main(sys.argv[1])))
