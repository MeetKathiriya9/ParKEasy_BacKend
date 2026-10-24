"""Fill a development database with demo facilities, floors, zones and spaces.

Run it from the `Server` directory:

    .venv\\Scripts\\python.exe -m scripts.seed_demo

Options:

    --reset   drop the demo collections first, so the run is repeatable
    --centre LAT,LNG   where to place the facilities (default: the client's
                       default map view, so the demo lines up with the app)

What it creates:

  * one operator account and one driver account, both with the password
    `parkeasy123`, so the client can be signed in without registering;
  * twelve facilities around the centre point, each with floors, zones and
    spaces, so the driver search has something real to find and the operator
    pages have a layout to edit.

The data is ordinary seeded content, not a fixture of the API: it goes through
the same document shapes the services write, so the search, the availability
tally and the layout grid all read it exactly as they would read production
data.
"""

from __future__ import annotations

import argparse
import asyncio
import random
from typing import Any

from bson import ObjectId

from app.core.config import Settings, get_settings
from app.core.security import hash_password
from app.db.client import close_mongo_connection, connect_to_mongo
from app.db.indexes import ensure_indexes
from app.models.enums import FacilityStatus, SpaceStatus, SpaceType
from app.models.facility import FACILITIES_COLLECTION
from app.models.space import FLOORS_COLLECTION, SPACES_COLLECTION, ZONES_COLLECTION
from app.models.user import USERS_COLLECTION
from app.utils.mongo import utcnow

DEFAULT_CENTRE = (22.5561, 72.9242)
DEMO_PASSWORD = "parkeasy123"

OPERATOR_EMAIL = "operator@parkeasy.dev"
DRIVER_EMAIL = "driver@parkeasy.dev"

# The collections this script owns. `--reset` drops exactly these and nothing
# else, so a careless run cannot take a real database with it.
DEMO_COLLECTIONS = (
    USERS_COLLECTION,
    FACILITIES_COLLECTION,
    FLOORS_COLLECTION,
    ZONES_COLLECTION,
    SPACES_COLLECTION,
)

# name, address, city, (lat offset, lng offset), floors, spaces per floor,
# hourly rate, amenities, rating, review count, open 24h
FACILITY_SEEDS: list[dict[str, Any]] = [
    {
        "name": "Riverfront Multi-Storey",
        "address": "12 Station Road, near the clock tower",
        "city": "Ahmedabad",
        "offset": (0.0021, -0.0014),
        "floors": 2,
        "spaces_per_floor": 60,
        "hourlyRate": 30.0,
        "amenities": ["ev_charging", "covered", "security", "cameras", "elevator"],
        "rating": 4.6,
        "reviewCount": 214,
        "open24h": True,
    },
    {
        "name": "Central Station Parking",
        "address": "Railway Station Road",
        "city": "Ahmedabad",
        "offset": (-0.0016, 0.0022),
        "floors": 1,
        "spaces_per_floor": 45,
        "hourlyRate": 45.0,
        "amenities": ["security", "cameras", "restrooms"],
        "rating": 3.9,
        "reviewCount": 88,
        "open24h": False,
        "openingTime": "06:00",
        "closingTime": "23:00",
    },
    {
        "name": "Business Park Tower",
        "address": "Corporate Road, Business Park",
        "city": "Ahmedabad",
        "offset": (0.0034, 0.0029),
        "floors": 3,
        "spaces_per_floor": 40,
        "hourlyRate": 25.0,
        "amenities": ["covered", "ev_charging", "valet", "elevator"],
        "rating": 4.4,
        "reviewCount": 156,
        "open24h": False,
        "openingTime": "07:00",
        "closingTime": "22:00",
    },
    {
        "name": "Market Square Open Lot",
        "address": "Market Square, old town",
        "city": "Ahmedabad",
        "offset": (-0.0028, -0.0031),
        "floors": 1,
        "spaces_per_floor": 30,
        "hourlyRate": 15.0,
        "amenities": ["security"],
        "rating": 3.4,
        "reviewCount": 41,
        "open24h": True,
    },
    {
        "name": "Airport Express Park",
        "address": "Airport Expressway, service road",
        "city": "Ahmedabad",
        "offset": (0.0075, -0.0052),
        "floors": 2,
        "spaces_per_floor": 35,
        "hourlyRate": 20.0,
        "amenities": ["ev_charging", "covered", "restrooms", "cameras"],
        "rating": 4.1,
        "reviewCount": 97,
        "open24h": True,
    },
    {
        "name": "University Campus Parking",
        "address": "University Road, campus gate 2",
        "city": "Ahmedabad",
        "offset": (-0.0061, 0.0044),
        "floors": 1,
        "spaces_per_floor": 50,
        "hourlyRate": 10.0,
        "amenities": ["covered", "cameras"],
        "rating": 4.2,
        "reviewCount": 132,
        "open24h": False,
        "openingTime": "06:30",
        "closingTime": "21:00",
    },
    {
        "name": "Mall Basement Parking",
        "address": "Highway Mall, ring road",
        "city": "Ahmedabad",
        "offset": (0.0048, 0.0057),
        "floors": 2,
        "spaces_per_floor": 55,
        "hourlyRate": 35.0,
        "amenities": ["covered", "ev_charging", "valet", "restrooms", "elevator"],
        "rating": 4.3,
        "reviewCount": 268,
        "open24h": True,
    },
    {
        "name": "Old Town Heritage Lot",
        "address": "Heritage Lane, near the temple",
        "city": "Ahmedabad",
        "offset": (-0.0044, 0.0011),
        "floors": 1,
        "spaces_per_floor": 18,
        "hourlyRate": 12.0,
        "amenities": ["accessible", "security"],
        "rating": 3.1,
        "reviewCount": 23,
        "open24h": False,
        "openingTime": "08:00",
        "closingTime": "20:00",
    },
    {
        "name": "Tech Park Phase 2",
        "address": "Software Park Road, phase 2",
        "city": "Ahmedabad",
        "offset": (0.0092, 0.0038),
        "floors": 3,
        "spaces_per_floor": 38,
        "hourlyRate": 18.0,
        "amenities": ["ev_charging", "covered", "cameras", "elevator", "security"],
        "rating": 4.5,
        "reviewCount": 183,
        "open24h": True,
    },
    {
        "name": "General Hospital Parking",
        "address": "Hospital Road",
        "city": "Ahmedabad",
        "offset": (-0.0073, -0.0026),
        "floors": 1,
        "spaces_per_floor": 25,
        "hourlyRate": 8.0,
        "amenities": ["accessible", "security", "cameras"],
        "rating": 3.7,
        "reviewCount": 54,
        "open24h": True,
    },
    {
        "name": "Riverside Commercial Garage",
        "address": "Riverside Commercial Complex",
        "city": "Ahmedabad",
        "offset": (0.0012, 0.0049),
        "floors": 2,
        "spaces_per_floor": 48,
        "hourlyRate": 28.0,
        "amenities": ["covered", "security", "restrooms", "accessible"],
        "rating": 4.0,
        "reviewCount": 121,
        "open24h": False,
        "openingTime": "06:00",
        "closingTime": "23:30",
    },
    {
        "name": "Suburban Village Lot",
        "address": "Village road, near the water tank",
        "city": "Ahmedabad",
        "offset": (0.0126, -0.0094),
        "floors": 1,
        "spaces_per_floor": 20,
        "hourlyRate": 5.0,
        "amenities": [],
        "rating": 2.9,
        "reviewCount": 12,
        "open24h": True,
    },
]

# The mix of space types in a zone. A real garage is mostly standard bays with
# a handful of the others, and the search's "parking type" filter is only
# interesting when the rare types actually exist.
SPACE_TYPE_MIX: list[tuple[SpaceType, int]] = [
    (SpaceType.STANDARD, 8),
    (SpaceType.COMPACT, 2),
    (SpaceType.EV, 1),
    (SpaceType.ACCESSIBLE, 1),
]


def _coordinates(lat: float, lng: float) -> tuple[float, float]:
    """The GeoJSON Point for a coordinate, in `[lng, lat]` order (RFC 7946)."""
    return lng, lat


async def _seed_user(db: Any, *, email: str, name: str, role: str) -> ObjectId:
    """Create a demo account, or return the id of the one already there."""
    existing = await db[USERS_COLLECTION].find_one({"email": email})
    if existing is not None:
        return existing["_id"]

    now = utcnow()
    document = {
        "_id": ObjectId(),
        "name": name,
        "email": email,
        "phone": None,
        "passwordHash": hash_password(DEMO_PASSWORD),
        "role": role,
        "status": "ACTIVE",
        "facilityIds": [],
        "createdAt": now,
        "updatedAt": now,
        "passwordChangedAt": now,
    }
    await db[USERS_COLLECTION].insert_one(document)
    return document["_id"]


async def _seed_facility(
    db: Any,
    *,
    operator_id: ObjectId,
    seed: dict[str, Any],
    centre: tuple[float, float],
    rng: random.Random,
) -> ObjectId:
    """One facility with its floors, zones and spaces."""
    now = utcnow()
    lat_offset, lng_offset = seed["offset"]
    lat = centre[0] + lat_offset
    lng = centre[1] + lng_offset

    facility_id = ObjectId()
    facility = {
        "_id": facility_id,
        "operatorId": operator_id,
        "name": seed["name"],
        "description": f"{seed['name']} - demo facility.",
        "address": seed["address"],
        "city": seed["city"],
        "location": {"type": "Point", "coordinates": _coordinates(lat, lng)},
        "open24h": seed["open24h"],
        "openingTime": seed.get("openingTime"),
        "closingTime": seed.get("closingTime"),
        "amenities": list(seed["amenities"]),
        "hourlyRate": seed["hourlyRate"],
        "dailyMax": seed["hourlyRate"] * 12,
        "rating": seed["rating"],
        "reviewCount": seed["reviewCount"],
        "status": FacilityStatus.ACTIVE.value,
        "createdAt": now,
        "updatedAt": now,
    }
    await db[FACILITIES_COLLECTION].insert_one(facility)

    floor_documents: list[dict[str, Any]] = []
    zone_documents: list[dict[str, Any]] = []
    space_documents: list[dict[str, Any]] = []

    for floor_index in range(seed["floors"]):
        level = floor_index
        floor_id = ObjectId()
        floor_name = "Ground Floor" if level == 0 else f"Level {level}"
        floor_documents.append(
            {
                "_id": floor_id,
                "facilityId": facility_id,
                "name": floor_name,
                "level": level,
                "createdAt": now,
                "updatedAt": now,
            }
        )

        zone_count = 2
        for zone_index in range(zone_count):
            zone_id = ObjectId()
            zone_name = f"Zone {chr(ord('A') + zone_index)}"
            zone_documents.append(
                {
                    "_id": zone_id,
                    "facilityId": facility_id,
                    "floorId": floor_id,
                    "name": zone_name,
                    "status": "ACTIVE",
                    "createdAt": now,
                    "updatedAt": now,
                }
            )

            number = 1
            for space_type, type_count in SPACE_TYPE_MIX:
                for _ in range(type_count):
                    if number > seed["spaces_per_floor"]:
                        break
                    space_documents.append(
                        {
                            "_id": ObjectId(),
                            "facilityId": facility_id,
                            "floorId": floor_id,
                            "floorName": floor_name,
                            "zoneId": zone_id,
                            "zoneName": zone_name,
                            "spaceNumber": f"{zone_name[-1]}-{number:02d}",
                            "type": space_type.value,
                            # A believable spread: mostly free, a few taken, a
                            # couple out of service.
                            "status": rng.choices(
                                [
                                    SpaceStatus.AVAILABLE.value,
                                    SpaceStatus.OCCUPIED.value,
                                    SpaceStatus.RESERVED.value,
                                    SpaceStatus.MAINTENANCE.value,
                                ],
                                weights=[62, 26, 8, 4],
                            )[0],
                            "reservable": True,
                            "createdAt": now,
                            "updatedAt": now,
                        }
                    )
                    number += 1

    if floor_documents:
        await db[FLOORS_COLLECTION].insert_many(floor_documents)
    if zone_documents:
        await db[ZONES_COLLECTION].insert_many(zone_documents)
    if space_documents:
        await db[SPACES_COLLECTION].insert_many(space_documents)

    return facility_id


async def seed(settings: Settings, *, reset: bool, centre: tuple[float, float]) -> None:
    db = await connect_to_mongo(settings)
    try:
        await ensure_indexes(db)

        if reset:
            for collection in DEMO_COLLECTIONS:
                await db[collection].delete_many({})
            print(f"Cleared {', '.join(DEMO_COLLECTIONS)}")

        operator_id = await _seed_user(
            db, email=OPERATOR_EMAIL, name="Demo Operator", role="operator"
        )
        await _seed_user(db, email=DRIVER_EMAIL, name="Demo Driver", role="driver")

        rng = random.Random(20260101)
        for seed_row in FACILITY_SEEDS:
            await _seed_facility(
                db, operator_id=operator_id, seed=seed_row, centre=centre, rng=rng
            )

        facilities = await db[FACILITIES_COLLECTION].count_documents({})
        spaces = await db[SPACES_COLLECTION].count_documents({})
        print(f"Seeded {facilities} facilities and {spaces} spaces")
        print(f"Sign in as {OPERATOR_EMAIL} or {DRIVER_EMAIL} with password {DEMO_PASSWORD}")
        print(f"Search centre: {centre[0]:.4f}, {centre[1]:.4f}")
    finally:
        # The client belongs to this event loop, so it has to be closed here
        # rather than in a second `asyncio.run`.
        await close_mongo_connection()


def _parse_centre(value: str) -> tuple[float, float]:
    parts = value.split(",")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("centre must be given as LAT,LNG")
    try:
        return float(parts[0]), float(parts[1])
    except ValueError as exc:
        raise argparse.ArgumentTypeError("centre must be two numbers") from exc


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reset",
        action="store_true",
        help="clear the demo collections before seeding",
    )
    parser.add_argument(
        "--centre",
        type=_parse_centre,
        default=DEFAULT_CENTRE,
        help="LAT,LNG to place the facilities around",
    )
    args = parser.parse_args()

    settings = get_settings()
    asyncio.run(seed(settings, reset=args.reset, centre=args.centre))


if __name__ == "__main__":
    main()
