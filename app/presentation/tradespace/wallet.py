"""Read-only projection of the existing authorized platform-wallet reader."""
from sqlalchemy import select
from app.models import User
from app.services.platform_wallet import get_active_platform_wallet, list_platform_wallet_history


async def load_wallet(request, db, user, base):
    if user.role not in {"superadmin", "admin", "operator", "trader"}:
        return None
    from app.web.routes import _require_platform_wallet_reader
    await _require_platform_wallet_reader(request, db)
    active = await get_active_platform_wallet(db)
    history = await list_platform_wallet_history(db) if user.role in {"superadmin", "admin"} else []
    actor_ids = {actor for row in history for actor in (row.created_by,row.deactivated_by) if actor}
    labels = dict((await db.execute(select(User.id,User.email).where(User.id.in_(actor_ids)))).all()) if actor_ids else {}
    return {"active": active, "history": history, "actors": labels,
            "qr_url": f"{base}/platform-wallet/qr?v={active.version}" if active else ""}
