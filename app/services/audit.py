"""Critical-action trail (DOC section 31: "Log security-sensitive actions").

Writes to the `auditLogs` collection, whose `actor_ts_1` and `entity_ts_1`
indexes already exist in `app.db.indexes`. Kept deliberately small and
forgiving: auditing must never be the reason a request fails, so every error
here is logged and swallowed.

`actorId` is deliberately nullable. Some of the most security-relevant events
happen with nobody authenticated -- a password reset requested for an unknown
address, a token presented for a user that no longer exists -- and forcing an
actor there would mean either dropping those events or inventing a fake id.
"""

from __future__ import annotations

import logging
from typing import Any

from bson import ObjectId

from app.utils.mongo import utcnow

logger = logging.getLogger(__name__)

AUDIT_LOGS_COLLECTION = "auditLogs"


async def record(
    db: Any,
    *,
    action: str,
    entity: str,
    actor_id: ObjectId | str | None = None,
    entity_id: ObjectId | str | None = None,
    ip: str | None = None,
    outcome: str = "SUCCESS",
    metadata: dict[str, Any] | None = None,
) -> None:
    """Append one audit row.

    `metadata` must never contain passwords, tokens or hashes; callers are
    responsible for that. Failures are logged, never raised.
    """
    entry: dict[str, Any] = {
        "entity": entity,
        "action": action,
        "outcome": outcome,
        "timestamp": utcnow(),
    }
    if actor_id is not None:
        entry["actorId"] = actor_id
    if entity_id is not None:
        entry["entityId"] = str(entity_id)
    if ip is not None:
        entry["ip"] = ip
    if metadata:
        entry["metadata"] = metadata

    try:
        await db[AUDIT_LOGS_COLLECTION].insert_one(dict(entry))
    except Exception:
        logger.exception("audit write failed for action=%s entity=%s", action, entity)
