"""The `staffAssignments` collection (DOC sections 7/10/11/18).

DOC section 18 does not name an assignment collection, but DOC sections 7, 10
and 11 require operators to assign staff to facilities, change those
assignments, and keep a history. A row per assignment *period* is the smallest
shape that answers all three:

* one row per `(staffId, facilityId)` pair while the assignment is `ACTIVE`;
* deactivating closes the row (`INACTIVE` + who/when/why) instead of deleting
  it, so the history survives;
* re-assigning the pair starts a new row, so two separate periods do not
  collapse into one.

`users.facilityIds` stays the fast authorization cache (it is read on every
request), and the service recomputes it from the active rows whenever an
assignment changes. The rows here are the source of truth; the cache is
derived.

`operatorId` is copied from the facility at assignment time. Facility ownership
never changes (`app.services.facilities`), so copying it keeps operator-scoped
queries to a single collection and survives the facility being renamed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, NotRequired, TypedDict

from bson import ObjectId

from app.models.enums import AssignmentStatus

STAFF_ASSIGNMENTS_COLLECTION = "staffAssignments"

MAX_ASSIGNMENT_NOTE_LENGTH = 200


class StaffAssignmentDocument(TypedDict):
    """A row of the `staffAssignments` collection."""

    _id: ObjectId
    staffId: ObjectId
    facilityId: ObjectId
    operatorId: ObjectId
    status: AssignmentStatus
    assignedBy: ObjectId
    assignedAt: datetime
    createdAt: datetime
    updatedAt: datetime
    deactivatedBy: NotRequired[ObjectId | None]
    deactivatedAt: NotRequired[datetime | None]
    reason: NotRequired[str | None]


def public_assignment(
    document: dict[str, Any],
    *,
    facility_name: str | None = None,
    facility_status: str | None = None,
    assigned_by_name: str | None = None,
    deactivated_by_name: str | None = None,
) -> dict[str, Any]:
    """Project an assignment row for the API.

    Names are resolved by the caller from the `parkingFacilities` and `users`
    collections, exactly like `public_facility` treats `operatorName`, so this
    module stays free of cross-collection reads.
    """
    return {
        "id": str(document["_id"]),
        "staffId": str(document["staffId"]),
        "facilityId": str(document["facilityId"]),
        "facilityName": facility_name,
        "facilityStatus": facility_status,
        "operatorId": str(document["operatorId"]) if document.get("operatorId") else None,
        "status": document.get("status"),
        "assignedById": (
            str(document["assignedBy"]) if document.get("assignedBy") else None
        ),
        "assignedByName": assigned_by_name,
        "assignedAt": document.get("assignedAt"),
        "deactivatedById": (
            str(document["deactivatedBy"]) if document.get("deactivatedBy") else None
        ),
        "deactivatedByName": deactivated_by_name,
        "deactivatedAt": document.get("deactivatedAt"),
        "reason": document.get("reason"),
    }
