"""Enumerations from DOC section 17 ("Suggested Parking States").

Values are stored in MongoDB using the exact uppercase spelling from the
requirements document, because operators and the client UI both read these
strings. The lowercase client literals in `Client/src/types/index.ts` are
mapped onto these at the API boundary (see `app/schemas/common.py`).
"""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    """The four roles the client navigation is built around."""

    DRIVER = "driver"
    STAFF = "staff"
    OPERATOR = "operator"
    ADMIN = "admin"


class UserStatus(StrEnum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    PENDING = "PENDING"


class FacilityStatus(StrEnum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    TEMPORARILY_CLOSED = "TEMPORARILY_CLOSED"
    MAINTENANCE = "MAINTENANCE"


class SpaceStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    RESERVED = "RESERVED"
    OCCUPIED = "OCCUPIED"
    MAINTENANCE = "MAINTENANCE"
    BLOCKED = "BLOCKED"


class SpaceType(StrEnum):
    STANDARD = "standard"
    COMPACT = "compact"
    LARGE = "large"
    EV = "ev"
    ACCESSIBLE = "accessible"


class ReservationStatus(StrEnum):
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    ARRIVED = "ARRIVED"
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    NO_SHOW = "NO_SHOW"


class SessionStatus(StrEnum):
    ACTIVE = "ACTIVE"
    EXTENDED = "EXTENDED"
    COMPLETED = "COMPLETED"
    ABORTED = "ABORTED"


class PaymentStatus(StrEnum):
    INITIATED = "INITIATED"
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    REFUNDED = "REFUNDED"


class ComplaintStatus(StrEnum):
    OPEN = "OPEN"
    ASSIGNED = "ASSIGNED"
    IN_PROGRESS = "IN_PROGRESS"
    RESOLVED = "RESOLVED"
    REJECTED = "REJECTED"
    ESCALATED = "ESCALATED"


class ViolationType(StrEnum):
    OVERSTAY = "overstay"
    WRONG_ZONE = "wrong_zone"
    UNAUTHORIZED = "unauthorized"
    RESERVED_MISUSE = "reserved_misuse"
    NO_PERMIT = "no_permit"


class ViolationStatus(StrEnum):
    OPEN = "OPEN"
    APPEALED = "APPEALED"
    RESOLVED = "RESOLVED"
    FINED = "FINED"


class ChargerStatus(StrEnum):
    AVAILABLE = "available"
    IN_USE = "in_use"
    MAINTENANCE = "maintenance"
    OUT_OF_SERVICE = "out_of_service"


class EventStatus(StrEnum):
    PLANNED = "PLANNED"
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class ReviewCategory(StrEnum):
    FACILITY = "facility"
    EV_CHARGING = "ev_charging"
    ACCESSIBILITY = "accessibility"
    SERVICE = "service"
    SAFETY = "safety"
    VALUE = "value"


class NotificationType(StrEnum):
    RESERVATION_CONFIRMED = "reservation_confirmed"
    REMINDER = "reminder"
    ARRIVAL_WINDOW = "arrival_window"
    EXPIRY = "expiry"
    NO_SHOW = "no_show"
    HIGH_OCCUPANCY = "high_occupancy"
    PAYMENT_SUCCESS = "payment_success"
    PAYMENT_FAILURE = "payment_failure"
    FACILITY_CLOSURE = "facility_closure"
    COMPLAINT_UPDATE = "complaint_update"
    VIOLATION = "violation"
    WAITLIST_AVAILABLE = "waitlist_available"


class PricingRuleType(StrEnum):
    HOURLY = "hourly"
    DAILY = "daily"
    PEAK = "peak"
    EVENT = "event"
    OVERNIGHT = "overnight"
    DYNAMIC = "dynamic"


# --- DOC section 18: `vehicles` key fields ---------------------------------


class VehicleType(StrEnum):
    """Body style. The DOC lists `type` as a key field but does not enumerate
    values, so this set covers the categories a parking facility cares about
    (clearance, bay size) plus an escape hatch for anything else."""

    SEDAN = "sedan"
    SUV = "suv"
    HATCHBACK = "hatchback"
    MOTORCYCLE = "motorcycle"
    TRUCK = "truck"
    VAN = "van"
    OTHER = "other"


class FuelType(StrEnum):
    """Fuel/energy. The DOC lists `fuelType` as a key field without values."""

    PETROL = "petrol"
    DIESEL = "diesel"
    ELECTRIC = "electric"
    HYBRID = "hybrid"
    CNG = "cng"
    OTHER = "other"
