"""PAMM permissions — managers are ADMIN-ASSIGNED (users.pamm_manager)."""
from fastapi import HTTPException


def is_admin(user: dict) -> bool:
    return (user or {}).get("role") == "admin"


async def is_manager(db, user: dict) -> bool:
    if is_admin(user):
        return True
    from bson import ObjectId
    doc = await db.users.find_one({"_id": ObjectId(user["id"])},
                                  {"pamm_manager": 1})
    return bool((doc or {}).get("pamm_manager"))


def require_admin(user: dict) -> None:
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="Admin only")


async def require_manager(db, user: dict) -> None:
    if not await is_manager(db, user):
        raise HTTPException(status_code=403,
                            detail="Strategy manager role required")


async def require_program_access(db, user: dict, program: dict) -> None:
    """Managers may only touch their own programs; admins touch all."""
    if is_admin(user):
        return
    if program.get("manager_id") != user["id"] or not await is_manager(db, user):
        raise HTTPException(status_code=403,
                            detail="Not the manager of this program")
