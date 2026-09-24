from sqlalchemy.ext.asyncio import AsyncSession
from app.models import AuditLog
async def audit(db: AsyncSession, action: str, target_type: str, actor_id=None, target_id=None, ip=None, details=None):
    db.add(AuditLog(action=action, target_type=target_type, actor_id=actor_id, target_id=str(target_id) if target_id else None, ip=ip, details=details or {}))
