"""The `users` collection and the `revokedTokens` revocation list.

Phase 1 keeps user documents in MongoDB (DOC section 6) as plain dicts so the
rest of the codebase can use PyMongo directly; the shapes are described here
with TypedDicts to get editor completion and to document the contract.

The user document follows the DOC section 12 account fields: name, email,
phone, passwordHash, role, status and a bcrypt-salted password.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, NotRequired, TypedDict

from bson import ObjectId

from app.models.enums import Role, UserStatus

USERS_COLLECTION = "users"
REVOKED_TOKENS_COLLECTION = "revokedTokens"


class UserDocument(TypedDict):
    """A row of the `users` collection.

    `_id` is a real MongoDB `ObjectId`, like every other collection, so it can
    be passed straight to `app.utils.mongo.to_object_id` when joining against
    `userId` / `ownerId` / `actorId` references elsewhere.

    `passwordHash` is a bcrypt string; the plain password is never persisted.
    `facilityIds` scopes staff access (DOC section 31) and is filled in when an
    admin assigns facilities later.
    """

    _id: ObjectId
    name: str
    email: str
    phone: str | None
    passwordHash: str
    role: Role
    status: UserStatus
    facilityIds: list[str]
    createdAt: datetime
    updatedAt: datetime
    lastLoginAt: NotRequired[datetime]
    createdBy: NotRequired[ObjectId]


class RevokedTokenDocument(TypedDict):
    """A row of the `revokedTokens` denylist.

    The `_id` here is the token's `jti` string rather than an `ObjectId`: the
    `jti` is already a unique random value and is what lookups are keyed on, so
    a natural key avoids a pointless extra index.

    Holds the token until it would have expired anyway. The TTL index on `exp`
    removes the row automatically, so the collection never needs a sweeper job.
    """

    _id: str
    jti: str
    userId: ObjectId
    exp: datetime
    revokedAt: datetime
    reason: NotRequired[str]


def public_user(document: dict[str, Any]) -> dict[str, Any]:
    """Project a user document down to the fields safe to send to a client.

    Guards against a `passwordHash` ever leaking through a future refactor that
    returns the raw document from a route. `_id` is stringified here so the
    `ObjectId` becomes a 24-character hex string over the wire.
    """
    return {
        "id": str(document["_id"]),
        "name": document.get("name"),
        "email": document.get("email"),
        "phone": document.get("phone"),
        "role": document.get("role"),
        "status": document.get("status"),
        "facilityIds": list(document.get("facilityIds") or []),
        "createdAt": document.get("createdAt"),
        "updatedAt": document.get("updatedAt"),
        "lastLoginAt": document.get("lastLoginAt"),
    }
