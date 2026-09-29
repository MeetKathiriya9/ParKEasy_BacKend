"""Index definitions, created on application startup.

Geospatial requirement (DOC section 19): parking facilities store a GeoJSON
Point under `location` and carry a `2dsphere` index so that `$near` / `$geoWithin`
queries are served by MongoDB rather than by application code.
"""

from __future__ import annotations

import logging

from pymongo import ASCENDING, DESCENDING, GEOSPHERE, IndexModel
from pymongo.asynchronous.database import AsyncDatabase

logger = logging.getLogger(__name__)

# collection name -> list of indexes
INDEX_SPECS: dict[str, list[IndexModel]] = {
    "users": [
        IndexModel([("email", ASCENDING)], unique=True, name="uniq_email"),
        IndexModel([("role", ASCENDING)], name="role_1"),
        IndexModel([("status", ASCENDING)], name="status_1"),
    ],
    "vehicles": [
        IndexModel([("userId", ASCENDING)], name="userId_1"),
        IndexModel(
            [("userId", ASCENDING), ("registrationNumber", ASCENDING)],
            unique=True,
            name="uniq_user_plate",
        ),
    ],
    "parkingFacilities": [
        # DOC section 19 - mandatory geospatial index
        IndexModel([("location", GEOSPHERE)], name="location_2dsphere"),
        IndexModel([("operatorId", ASCENDING)], name="operatorId_1"),
        IndexModel([("status", ASCENDING)], name="status_1"),
        IndexModel([("name", ASCENDING)], name="name_1"),
    ],
    "parkingSpaces": [
        IndexModel(
            [("facilityId", ASCENDING), ("spaceNumber", ASCENDING)],
            unique=True,
            name="uniq_facility_space",
        ),
        IndexModel([("facilityId", ASCENDING), ("status", ASCENDING)], name="facility_status_1"),
    ],
    "reservations": [
        IndexModel(
            [("userId", ASCENDING), ("startTime", DESCENDING)],
            name="user_start_1",
        ),
        IndexModel(
            [("facilityId", ASCENDING), ("status", ASCENDING)],
            name="facility_status_1",
        ),
        IndexModel([("qrToken", ASCENDING)], unique=True, sparse=True, name="uniq_qr"),
        IndexModel([("endTime", ASCENDING), ("status", ASCENDING)], name="expiry_sweep_1"),
    ],
    "parkingSessions": [
        IndexModel([("reservationId", ASCENDING)], unique=True, sparse=True, name="uniq_res"),
        IndexModel([("facilityId", ASCENDING), ("status", ASCENDING)], name="facility_status_1"),
        IndexModel([("entryTime", DESCENDING)], name="entry_1"),
    ],
    "payments": [
        IndexModel([("reservationId", ASCENDING)], name="reservation_1"),
        IndexModel([("userId", ASCENDING), ("createdAt", DESCENDING)], name="user_created_1"),
        IndexModel(
            [("provider", ASCENDING), ("providerTransactionId", ASCENDING)],
            sparse=True,
            name="provider_txn_1",
        ),
    ],
    "pricingRules": [
        IndexModel([("facilityId", ASCENDING), ("active", ASCENDING)], name="facility_active_1"),
    ],
    "occupancyLogs": [
        IndexModel(
            [("facilityId", ASCENDING), ("timestamp", DESCENDING)],
            name="facility_timestamp_1",
        ),
    ],
    "violations": [
        IndexModel([("facilityId", ASCENDING), ("status", ASCENDING)], name="facility_status_1"),
        IndexModel([("vehicleId", ASCENDING)], sparse=True, name="vehicle_1"),
    ],
    "evChargers": [
        IndexModel(
            [("facilityId", ASCENDING), ("chargerNumber", ASCENDING)],
            unique=True,
            name="uniq_facility_charger",
        ),
    ],
    "evSessions": [
        IndexModel([("chargerId", ASCENDING), ("startTime", DESCENDING)], name="charger_start_1"),
    ],
    "notifications": [
        IndexModel([("recipientId", ASCENDING), ("readAt", ASCENDING)], name="recipient_read_1"),
    ],
    "reviews": [
        IndexModel(
            [("facilityId", ASCENDING), ("createdAt", DESCENDING)],
            name="facility_created_1",
        ),
        IndexModel(
            [("userId", ASCENDING), ("facilityId", ASCENDING)],
            unique=True,
            name="uniq_user_facility",
        ),
    ],
    "complaints": [
        IndexModel([("userId", ASCENDING), ("createdAt", DESCENDING)], name="user_created_1"),
        IndexModel([("status", ASCENDING), ("createdAt", ASCENDING)], name="status_created_1"),
    ],
    "events": [
        IndexModel([("facilityId", ASCENDING), ("startsAt", ASCENDING)], name="facility_starts_1"),
    ],
    "predictionResults": [
        IndexModel(
            [("facilityId", ASCENDING), ("targetTime", ASCENDING)],
            unique=True,
            name="uniq_facility_target",
        ),
    ],
    "favorites": [
        IndexModel(
            [("userId", ASCENDING), ("facilityId", ASCENDING)],
            unique=True,
            name="uniq_user_facility",
        ),
    ],
    "auditLogs": [
        IndexModel([("actorId", ASCENDING), ("timestamp", DESCENDING)], name="actor_ts_1"),
        IndexModel([("entity", ASCENDING), ("timestamp", DESCENDING)], name="entity_ts_1"),
    ],

    "revokedTokens": [
        IndexModel([("exp", ASCENDING)], expireAfterSeconds=0, name="ttl_exp"),
        IndexModel([("userId", ASCENDING)], name="userId_1"),
    ],
}


async def ensure_indexes(db: AsyncDatabase) -> None:
    """Create every index defined in `INDEX_SPECS`. Idempotent and safe to re-run."""
    for collection_name, models in INDEX_SPECS.items():
        try:
            await db[collection_name].create_indexes(models)
        except Exception:  # startup must not crash over a single collection
            logger.exception("Failed to create indexes for '%s'", collection_name)
            continue
        logger.info("Ensured %d index(es) on '%s'", len(models), collection_name)
    logger.info("Index bootstrap complete")
