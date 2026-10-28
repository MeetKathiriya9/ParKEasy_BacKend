"""Staff accounts, assignments and assignment history (DOC sections 7/10/11).

The authorization shape mirrors the facility group:

* an **operator** sees and manages only staff whose `operatorId` is theirs, and
  may assign those staff only to facilities the operator owns - that is what
  "Ravi can manage City Mall but not Airport Parking" means in practice;
* an **admin** sees and manages every staff member and may assign to any
  facility, or create a staff account on an operator's behalf;
* **staff** and **drivers** are refused: staff have their own portal, and this
  group is management.

A foreign, unknown or malformed staff id is a `404`, never a `403` that would
confirm the id exists - the same rule the facility and vehicle groups follow.

Assignments are append-only periods. `POST .../assignments` inserts an `ACTIVE`
row; `DELETE .../assignments/{id}` closes it as `INACTIVE` with who/when/why.
The database enforces one active row per `(staffId, facilityId)` pair, so two
simultaneous assignments cannot both succeed. `users.facilityIds` is recomputed
from the active rows after every change, which is what `get_current_user` reads
to authorize the staff member's very next request.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from typing import Any

from bson import ObjectId
from pymongo import ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from app.core.config import Settings
from app.core.errors import AppError, ConflictError, ErrorCode, NotFoundError
from app.core.security import hash_password
from app.models.enums import AssignmentStatus, Role, UserStatus
from app.models.facility import FACILITIES_COLLECTION
from app.models.staff import STAFF_ASSIGNMENTS_COLLECTION, public_assignment
from app.models.user import USERS_COLLECTION
from app.schemas.staff import (
    ChangeStaffStatusRequest,
    CreateAssignmentRequest,
    CreateStaffRequest,
    UpdateStaffRequest,
)
from app.services.auth import AuthErrorCode
from app.services.facilities import owned_facility
from app.utils.mongo import to_object_id, utcnow

logger = logging.getLogger(__name__)

STAFF_NOT_FOUND = "This staff member does not exist"
ASSIGNMENT_NOT_FOUND = "This assignment does not exist"
ACCOUNT_NOT_FOUND = "This account no longer exists"


class StaffErrorCode:
    """Conflict reasons carried in `error.code`, so a client can branch on it."""

    ASSIGNMENT_EXISTS = "ASSIGNMENT_EXISTS"
    STAFF_NOT_ACTIVE = "STAFF_NOT_ACTIVE"
    STAFF_LIMIT_REACHED = "STAFF_LIMIT_REACHED"
    OPERATOR_MISMATCH = "OPERATOR_MISMATCH"


# --- Scope helpers ----------------------------------------------------------


def _staff_scope_filter(user_id: str, role: Role) -> dict[str, Any]:
    """The query narrowing a staff listing to what the caller may see."""
    filters: dict[str, Any] = {"role": Role.STAFF.value}
    if role is not Role.ADMIN:
        owner = to_object_id(user_id)
        # An id that cannot be an ObjectId matches nothing rather than everyone.
        filters["operatorId"] = owner if owner is not None else {"$in": []}
    return filters


async def owned_staff(
    db: AsyncDatabase, *, user_id: str, role: Role, staff_id: str
) -> dict[str, Any]:
    """Load a staff member the caller may manage, or raise a 404."""
    object_id = to_object_id(staff_id)
    if object_id is None:
        raise NotFoundError(STAFF_NOT_FOUND)

    document = await db[USERS_COLLECTION].find_one({"_id": object_id})
    if document is None or document.get("role") != Role.STAFF.value:
        raise NotFoundError(STAFF_NOT_FOUND)

    if role is Role.ADMIN:
        return document
    if str(document.get("operatorId")) != user_id:
        raise NotFoundError(STAFF_NOT_FOUND)
    return document


async def _operator_object_id(db: AsyncDatabase, account_id: str) -> ObjectId:
    """Resolve an owning operator account, or refuse.

    `404` when the account does not exist (same message as a bad id, so this
    cannot be used to probe for real accounts); `422` when it exists but is not
    an operator - the admin named a driver/admin by mistake, which is a request
    problem rather than a missing resource.
    """
    object_id = to_object_id(account_id)
    if object_id is None:
        raise NotFoundError(ACCOUNT_NOT_FOUND)

    account = await db[USERS_COLLECTION].find_one({"_id": object_id}, {"role": 1})
    if account is None:
        raise NotFoundError(ACCOUNT_NOT_FOUND)
    if account.get("role") != Role.OPERATOR.value:
        raise AppError(
            "operatorId must reference an operator account",
            code=ErrorCode.VALIDATION_ERROR,
            status_code=422,
            details={"field": "operatorId"},
        )
    return object_id


async def _owner_for_new_staff(
    db: AsyncDatabase, *, user_id: str, role: Role, requested: str | None
) -> ObjectId:
    """Work out which operator owns a staff account being created."""
    if role is Role.ADMIN:
        if requested is None:
            raise AppError(
                "operatorId is required when an admin creates a staff account",
                code=ErrorCode.VALIDATION_ERROR,
                status_code=422,
                details={"field": "operatorId"},
            )
        return await _operator_object_id(db, requested)

    if requested is not None:
        raise AppError(
            "Only an admin may set operatorId; an operator creates staff for themselves",
            code=ErrorCode.VALIDATION_ERROR,
            status_code=422,
            details={"field": "operatorId"},
        )

    owner = to_object_id(user_id)
    if owner is None:
        raise NotFoundError(ACCOUNT_NOT_FOUND)
    return owner


async def _facility_for_owner(
    db: AsyncDatabase,
    *,
    user_id: str,
    role: Role,
    facility_id: str,
    owner_id: ObjectId,
) -> dict[str, Any]:
    """An assignable facility that belongs to the staff member's operator."""
    document = await owned_facility(
        db, user_id=user_id, role=role, facility_id=facility_id
    )
    if str(document.get("operatorId")) != str(owner_id):
        raise AppError(
            "facilityIds must reference facilities owned by the staff member's operator",
            code=ErrorCode.VALIDATION_ERROR,
            status_code=422,
            details={"field": "facilityIds", "facilityId": facility_id},
        )
    return document


# --- Serialisation helpers --------------------------------------------------


async def _facility_map(
    db: AsyncDatabase, facility_ids: list[Any]
) -> dict[str, dict[str, Any]]:
    object_ids = [
        value if isinstance(value, ObjectId) else to_object_id(str(value))
        for value in facility_ids
    ]
    wanted = [value for value in object_ids if value is not None]
    if not wanted:
        return {}
    rows = [
        row
        async for row in db[FACILITIES_COLLECTION].find(
            {"_id": {"$in": wanted}}, {"name": 1, "status": 1}
        )
    ]
    return {str(row["_id"]): row for row in rows}


async def _user_names(db: AsyncDatabase, user_ids: list[Any]) -> dict[str, str]:
    object_ids = [
        value if isinstance(value, ObjectId) else to_object_id(str(value))
        for value in user_ids
    ]
    wanted = [value for value in object_ids if value is not None]
    if not wanted:
        return {}
    rows = [
        row
        async for row in db[USERS_COLLECTION].find(
            {"_id": {"$in": wanted}}, {"name": 1}
        )
    ]
    return {str(row["_id"]): row.get("name") or "" for row in rows}


async def _assignment_responses(
    db: AsyncDatabase, assignments: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    if not assignments:
        return []

    facilities = await _facility_map(db, [row.get("facilityId") for row in assignments])
    actor_ids = [
        value
        for row in assignments
        for value in (row.get("assignedBy"), row.get("deactivatedBy"))
        if value is not None
    ]
    names = await _user_names(db, actor_ids)

    responses: list[dict[str, Any]] = []
    for row in assignments:
        facility = facilities.get(str(row.get("facilityId")), {})
        responses.append(
            public_assignment(
                row,
                facility_name=facility.get("name"),
                facility_status=facility.get("status"),
                assigned_by_name=names.get(str(row.get("assignedBy"))),
                deactivated_by_name=names.get(str(row.get("deactivatedBy"))),
            )
        )
    return responses


async def _active_assignments(
    db: AsyncDatabase, staff_ids: list[Any]
) -> dict[str, list[dict[str, Any]]]:
    if not staff_ids:
        return {}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    rows = [
        row
        async for row in db[STAFF_ASSIGNMENTS_COLLECTION].find(
            {"staffId": {"$in": staff_ids}, "status": AssignmentStatus.ACTIVE.value}
        )
    ]
    for row in rows:
        grouped[str(row["staffId"])].append(row)
    return grouped


def _staff_summary(
    document: dict[str, Any],
    *,
    active: list[dict[str, Any]],
    facilities: dict[str, dict[str, Any]],
    operator_names: dict[str, str],
) -> dict[str, Any]:
    facility_refs: list[dict[str, Any]] = []
    for row in active:
        facility = facilities.get(str(row.get("facilityId")), {})
        facility_refs.append(
            {
                "id": str(row["facilityId"]),
                "name": facility.get("name") or "Unknown facility",
                "status": facility.get("status") or "INACTIVE",
            }
        )
    facility_refs.sort(key=lambda item: item["name"].lower())

    owner = document.get("operatorId")
    return {
        "id": str(document["_id"]),
        "name": document.get("name") or "",
        "email": document.get("email") or "",
        "phone": document.get("phone"),
        "status": document.get("status") or UserStatus.ACTIVE.value,
        "operatorId": str(owner) if owner else None,
        "operatorName": operator_names.get(str(owner)) if owner else None,
        "facilityIds": sorted(item["id"] for item in facility_refs),
        "facilities": facility_refs,
        "createdAt": document.get("createdAt"),
        "updatedAt": document.get("updatedAt"),
        "lastLoginAt": document.get("lastLoginAt"),
    }


async def _summaries(
    db: AsyncDatabase, documents: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    if not documents:
        return []
    active_by_staff = await _active_assignments(db, [doc["_id"] for doc in documents])
    rows = [row for rows in active_by_staff.values() for row in rows]
    facilities = await _facility_map(db, [row.get("facilityId") for row in rows])
    operator_names = await _user_names(
        db, [doc.get("operatorId") for doc in documents if doc.get("operatorId")]
    )
    return [
        _staff_summary(
            document,
            active=active_by_staff.get(str(document["_id"]), []),
            facilities=facilities,
            operator_names=operator_names,
        )
        for document in documents
    ]


async def _detail(db: AsyncDatabase, document: dict[str, Any]) -> dict[str, Any]:
    rows = [
        row
        async for row in db[STAFF_ASSIGNMENTS_COLLECTION]
        .find({"staffId": document["_id"]})
        .sort("assignedAt", -1)
    ]
    active = [row for row in rows if row.get("status") == AssignmentStatus.ACTIVE.value]
    facilities = await _facility_map(db, [row.get("facilityId") for row in rows])
    operator_names = await _user_names(
        db, [document.get("operatorId")] if document.get("operatorId") else []
    )
    summary = _staff_summary(
        document,
        active=active,
        facilities=facilities,
        operator_names=operator_names,
    )
    summary["assignments"] = await _assignment_responses(db, rows)
    return summary


# --- Facility assignment cache ---------------------------------------------


async def sync_facility_ids(db: AsyncDatabase, staff_id: ObjectId) -> list[str]:
    """Recompute `users.facilityIds` from the active assignment rows.

    The rows are the source of truth; the user field is the per-request cache
    `get_current_user` reads. Recomputing (rather than `$addToSet`/`$pull`)
    makes the cache self-healing: if one update is lost, the next assignment
    change repairs the whole list instead of preserving the error.
    """
    facility_ids = [
        str(row["facilityId"])
        async for row in db[STAFF_ASSIGNMENTS_COLLECTION].find(
            {"staffId": staff_id, "status": AssignmentStatus.ACTIVE.value},
            {"facilityId": 1},
        )
    ]
    ordered = sorted(set(facility_ids))
    await db[USERS_COLLECTION].update_one(
        {"_id": staff_id}, {"$set": {"facilityIds": ordered, "updatedAt": utcnow()}}
    )
    return ordered


async def _insert_assignment(
    db: AsyncDatabase,
    *,
    staff_id: ObjectId,
    facility: dict[str, Any],
    assigned_by: ObjectId,
    note: str | None = None,
) -> dict[str, Any]:
    now = utcnow()
    document: dict[str, Any] = {
        "staffId": staff_id,
        "facilityId": facility["_id"],
        "operatorId": facility["operatorId"],
        "status": AssignmentStatus.ACTIVE.value,
        "assignedBy": assigned_by,
        "assignedAt": now,
        "reason": note,
        "createdAt": now,
        "updatedAt": now,
    }
    try:
        await db[STAFF_ASSIGNMENTS_COLLECTION].insert_one(document)
    except DuplicateKeyError as exc:
        # The partial unique index is the real guard; this also covers two
        # simultaneous requests that both passed the pre-check.
        raise ConflictError(
            "This staff member is already assigned to that facility",
            code=StaffErrorCode.ASSIGNMENT_EXISTS,
            details={
                "staffId": str(staff_id),
                "facilityId": str(facility["_id"]),
            },
        ) from exc
    return document


# --- CRUD ------------------------------------------------------------------


async def list_staff(
    db: AsyncDatabase,
    *,
    user_id: str,
    role: Role,
    facility_id: str | None = None,
    status: UserStatus | None = None,
    query: str | None = None,
) -> list[dict[str, Any]]:
    """Staff the caller may see, with their active facility assignments."""
    filters = _staff_scope_filter(user_id, role)

    if status is not None:
        filters["status"] = status.value

    if query:
        pattern = re.escape(query.strip())
        filters["$or"] = [
            {"name": {"$regex": pattern, "$options": "i"}},
            {"email": {"$regex": pattern, "$options": "i"}},
        ]

    if facility_id is not None:
        # `owned_facility` scopes the operator to their own facilities and gives
        # everyone else the same 404 for a foreign id.
        facility = await owned_facility(
            db, user_id=user_id, role=role, facility_id=facility_id
        )
        assigned = await db[STAFF_ASSIGNMENTS_COLLECTION].distinct(
            "staffId",
            {
                "facilityId": {"$in": [facility["_id"], str(facility["_id"])]},
                "status": AssignmentStatus.ACTIVE.value,
            },
        )
        if not assigned:
            return []
        filters["_id"] = {"$in": assigned}

    documents = [
        row
        async for row in db[USERS_COLLECTION].find(filters).sort("name", 1)
    ]
    return await _summaries(db, documents)


async def get_staff(
    db: AsyncDatabase, *, user_id: str, role: Role, staff_id: str
) -> dict[str, Any]:
    document = await owned_staff(db, user_id=user_id, role=role, staff_id=staff_id)
    return await _detail(db, document)


async def create_staff(
    db: AsyncDatabase,
    *,
    user_id: str,
    role: Role,
    payload: CreateStaffRequest,
    settings: Settings,
) -> dict[str, Any]:
    owner_id = await _owner_for_new_staff(
        db, user_id=user_id, role=role, requested=payload.operatorId
    )

    existing = await db[USERS_COLLECTION].count_documents(
        {"role": Role.STAFF.value, "operatorId": owner_id}
    )
    if existing >= settings.staff_max_per_operator:
        raise ConflictError(
            "This operator already has the maximum number of staff accounts",
            code=StaffErrorCode.STAFF_LIMIT_REACHED,
            details={"max": settings.staff_max_per_operator},
        )

    # Resolve every initial facility before writing anything, so a bad id does
    # not leave a half-created account behind.
    facilities: list[dict[str, Any]] = []
    seen: set[str] = set()
    for facility_id in payload.facilityIds:
        facility = await _facility_for_owner(
            db,
            user_id=user_id,
            role=role,
            facility_id=facility_id,
            owner_id=owner_id,
        )
        key = str(facility["_id"])
        if key not in seen:
            seen.add(key)
            facilities.append(facility)

    now = utcnow()
    document: dict[str, Any] = {
        "name": payload.name,
        "email": payload.email,
        "phone": payload.phone,
        "passwordHash": hash_password(payload.password),
        "role": Role.STAFF.value,
        "status": UserStatus.ACTIVE.value,
        "facilityIds": [],
        "operatorId": owner_id,
        "createdBy": to_object_id(user_id),
        "createdAt": now,
        "updatedAt": now,
        "passwordChangedAt": now,
    }

    try:
        await db[USERS_COLLECTION].insert_one(document)
    except DuplicateKeyError as exc:
        raise ConflictError(
            "An account with that email already exists",
            code=AuthErrorCode.EMAIL_ALREADY_REGISTERED,
        ) from exc

    actor = to_object_id(user_id)
    if actor is None:  # unreachable for a real token; keeps the type honest
        raise NotFoundError(ACCOUNT_NOT_FOUND)

    for facility in facilities:
        await _insert_assignment(
            db,
            staff_id=document["_id"],
            facility=facility,
            assigned_by=actor,
        )

    await sync_facility_ids(db, document["_id"])
    refreshed = await db[USERS_COLLECTION].find_one({"_id": document["_id"]})
    logger.info("Created staff %s for operator %s", document["_id"], owner_id)
    return await _detail(db, refreshed or document)


async def update_staff(
    db: AsyncDatabase,
    *,
    user_id: str,
    role: Role,
    staff_id: str,
    payload: UpdateStaffRequest,
) -> tuple[dict[str, Any], bool]:
    document = await owned_staff(db, user_id=user_id, role=role, staff_id=staff_id)
    updates: dict[str, Any] = {}

    if "name" in payload.model_fields_set:
        updates["name"] = payload.name
    if "phone" in payload.model_fields_set:
        updates["phone"] = payload.phone

    if "operatorId" in payload.model_fields_set:
        if role is not Role.ADMIN:
            raise AppError(
                "Only an admin may change operatorId",
                code=ErrorCode.VALIDATION_ERROR,
                status_code=422,
                details={"field": "operatorId"},
            )
        if payload.operatorId is None:
            raise AppError(
                "operatorId cannot be null",
                code=ErrorCode.VALIDATION_ERROR,
                status_code=422,
                details={"field": "operatorId"},
            )
        target = await _operator_object_id(db, payload.operatorId)
        current = document.get("operatorId")
        if current is not None and str(current) != str(target):
            raise AppError(
                "operatorId cannot be changed once it is set",
                code=ErrorCode.VALIDATION_ERROR,
                status_code=422,
                details={"field": "operatorId"},
            )
        # Adoption is only safe when every existing active assignment already
        # belongs to the new owner; otherwise the operator would inherit access
        # to another operator's garages.
        foreign = await db[STAFF_ASSIGNMENTS_COLLECTION].count_documents(
            {
                "staffId": document["_id"],
                "status": AssignmentStatus.ACTIVE.value,
                "operatorId": {"$ne": target},
            }
        )
        if foreign:
            raise ConflictError(
                "This staff member has active assignments for another operator",
                code=StaffErrorCode.OPERATOR_MISMATCH,
                details={"assignments": foreign},
            )
        updates["operatorId"] = target

    if not updates:
        return document, False

    updates["updatedAt"] = utcnow()
    updated = await db[USERS_COLLECTION].find_one_and_update(
        {"_id": document["_id"]},
        {"$set": updates},
        return_document=ReturnDocument.AFTER,
    )
    return updated or document, True


async def set_staff_status(
    db: AsyncDatabase,
    *,
    user_id: str,
    role: Role,
    staff_id: str,
    payload: ChangeStaffStatusRequest,
) -> tuple[dict[str, Any], str]:
    """Set ACTIVE/SUSPENDED and return the document plus the previous status.

    Suspension deliberately leaves assignments alone: the rows and
    `facilityIds` stay as they were, so reactivation restores the same access
    without the operator having to remember which garages the member covered.
    The next request from a suspended member is refused by `get_current_user`,
    because it reads the stored status instead of trusting the token.
    """
    document = await owned_staff(db, user_id=user_id, role=role, staff_id=staff_id)
    previous = str(document.get("status") or UserStatus.ACTIVE.value)
    updated = await db[USERS_COLLECTION].find_one_and_update(
        {"_id": document["_id"]},
        {"$set": {"status": payload.status.value, "updatedAt": utcnow()}},
        return_document=ReturnDocument.AFTER,
    )
    return updated or document, previous


# --- Assignments -----------------------------------------------------------


async def list_assignments(
    db: AsyncDatabase,
    *,
    user_id: str,
    role: Role,
    staff_id: str,
    status: AssignmentStatus | None = None,
) -> list[dict[str, Any]]:
    document = await owned_staff(db, user_id=user_id, role=role, staff_id=staff_id)
    query: dict[str, Any] = {"staffId": document["_id"]}
    if status is not None:
        query["status"] = status.value
    rows = [
        row
        async for row in db[STAFF_ASSIGNMENTS_COLLECTION]
        .find(query)
        .sort("assignedAt", -1)
    ]
    return await _assignment_responses(db, rows)


async def assign_staff(
    db: AsyncDatabase,
    *,
    user_id: str,
    role: Role,
    staff_id: str,
    payload: CreateAssignmentRequest,
) -> dict[str, Any]:
    document = await owned_staff(db, user_id=user_id, role=role, staff_id=staff_id)
    if document.get("status") != UserStatus.ACTIVE.value:
        raise ConflictError(
            "Activate this staff member before assigning facilities",
            code=StaffErrorCode.STAFF_NOT_ACTIVE,
            details={"staffId": staff_id},
        )

    # Reuse the facility service's ownership rule: an operator assigning a
    # facility they do not own gets the same 404 as an unknown id.
    facility = await owned_facility(
        db, user_id=user_id, role=role, facility_id=payload.facilityId
    )

    actor = to_object_id(user_id)
    if actor is None:
        raise NotFoundError(ACCOUNT_NOT_FOUND)

    assignment = await _insert_assignment(
        db,
        staff_id=document["_id"],
        facility=facility,
        assigned_by=actor,
        note=payload.note,
    )
    await sync_facility_ids(db, document["_id"])
    responses = await _assignment_responses(db, [assignment])
    return responses[0]


async def unassign_staff(
    db: AsyncDatabase,
    *,
    user_id: str,
    role: Role,
    staff_id: str,
    assignment_id: str,
    reason: str | None = None,
) -> dict[str, Any]:
    document = await owned_staff(db, user_id=user_id, role=role, staff_id=staff_id)
    assignment_oid = to_object_id(assignment_id)
    if assignment_oid is None:
        raise NotFoundError(ASSIGNMENT_NOT_FOUND)

    actor = to_object_id(user_id)
    now = utcnow()
    updated = await db[STAFF_ASSIGNMENTS_COLLECTION].find_one_and_update(
        {
            "_id": assignment_oid,
            "staffId": document["_id"],
            "status": AssignmentStatus.ACTIVE.value,
        },
        {
            "$set": {
                "status": AssignmentStatus.INACTIVE.value,
                "deactivatedBy": actor,
                "deactivatedAt": now,
                "reason": reason,
                "updatedAt": now,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise NotFoundError(ASSIGNMENT_NOT_FOUND)

    await sync_facility_ids(db, document["_id"])
    responses = await _assignment_responses(db, [updated])
    return responses[0]


__all__ = [
    "StaffErrorCode",
    "assign_staff",
    "create_staff",
    "get_staff",
    "list_assignments",
    "list_staff",
    "owned_staff",
    "set_staff_status",
    "sync_facility_ids",
    "unassign_staff",
    "update_staff",
]
