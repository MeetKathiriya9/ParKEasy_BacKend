"""Domain models: enums and shared value types.

Collection *documents* will be modelled with PyMongo (not an ODM) - see
`app/db/client.py`. This module holds the state machines described in DOC
section 17 plus the client-facing role literals.
"""

from app.models.enums import (
    Amenity,
    ChargerStatus,
    ComplaintStatus,
    EventStatus,
    FacilityStatus,
    NotificationType,
    PaymentStatus,
    ReservationStatus,
    ReviewCategory,
    Role,
    SessionStatus,
    SpaceStatus,
    SpaceType,
    UserStatus,
    ViolationStatus,
    ViolationType,
    ZoneStatus,
)

__all__ = [
    "Amenity",
    "ChargerStatus",
    "ComplaintStatus",
    "EventStatus",
    "FacilityStatus",
    "NotificationType",
    "PaymentStatus",
    "ReservationStatus",
    "ReviewCategory",
    "Role",
    "SessionStatus",
    "SpaceStatus",
    "SpaceType",
    "UserStatus",
    "ViolationStatus",
    "ViolationType",
    "ZoneStatus",
]
