"""Discovery over facilities: nearby search, one facility's live availability.

This is the driver-facing read path (DOC sections 7, 19, 20, 22). It is
deliberately separate from `app.services.facilities`, which answers the
*management* side of the same collection and refuses drivers outright.

## How a nearby search runs

1. **The geography happens in the database.** A `$geoNear` stage over the
   `2dsphere` index returns every `ACTIVE` facility with its distance from the
   search point in metres. Closed and temporarily-closed facilities are
   excluded here, because DOC section 22 asks for "alternative facilities when
   a facility is closed" - a closed garage is not an alternative to itself.
2. **Availability is read in bulk.** One aggregation per query answers the
   per-status tally and the per-type breakdown for every candidate at once, so
   a search of forty facilities costs three queries, not forty.
3. **The filters run in Python.** Price, amenities, open-now and parking type
   are applied to the candidate set.
4. **The radius only orders the results, it does not hide the rest.** Facilities
   inside `radiusKm` form the first tier and are listed before anything
   farther away; the facilities outside the radius follow, nearest first. That
   is what lets a driver who is standing nowhere near a garage still see
   twelve facilities, with the closest ones on top.

Step 4 is why `$geoNear` no longer takes a `maxDistance`: the tier below the
radius needs the rows the radius would have discarded. The cost is a scan of
the `ACTIVE` collection per request, which is fine at this size and is the
obvious place to add a candidate cap if the collection grows.

The filter/sort/page split is left in Python because it keeps the pagination
total exact and the filter rules readable in one place - the same rules the
availability route uses, so search and detail can never disagree about what
"open now" means.
"""

from __future__ import annotations

import logging
from typing import Any

from pymongo.asynchronous.database import AsyncDatabase

from app.core.errors import NotFoundError
from app.models.enums import Amenity, FacilityStatus, SpaceStatus
from app.models.facility import (
    FACILITIES_COLLECTION,
    geojson_point,
    is_open_now,
    public_facility,
)
from app.models.space import SPACES_COLLECTION
from app.schemas.parking import ParkingSortBy
from app.services.facilities import (
    FACILITY_NOT_FOUND,
    operator_names,
    space_counts,
    space_type_counts,
)
from app.utils.mongo import to_object_id, utcnow

logger = logging.getLogger(__name__)

# The sort keys DOC section 22 asks for. `distance` is the default because a
# driver opening the search page is asking "what is near me", and the others
# are explicit choices.
_SORT_KEYS: dict[str, ParkingSortBy] = {
    "distance": "distance",
    "price": "price",
    "availability": "availability",
    "rating": "rating",
}


def normalise_sort(value: str | None) -> ParkingSortBy:
    """Map a client-supplied sort key onto a supported one."""
    if value is None:
        return "distance"
    key = value.strip().lower()
    return _SORT_KEYS.get(key, "distance")


async def _candidates(db: AsyncDatabase, *, lat: float, lng: float) -> list[dict[str, Any]]:
    """Every `ACTIVE` facility, each with `distanceMeters` from the point.

    There is deliberately no `maxDistance` here. The caller sorts the rows into
    an inside-the-radius tier and a farther tier, so a facility beyond the
    radius must still be known to the query - it is a top-up candidate, not a
    discarded one.
    """
    pipeline = [
        {
            "$geoNear": {
                "near": geojson_point(lat, lng),
                "distanceField": "distanceMeters",
                "spherical": True,
                "query": {"status": FacilityStatus.ACTIVE.value},
            }
        }
    ]
    rows: list[dict[str, Any]] = []
    async for row in await db[FACILITIES_COLLECTION].aggregate(pipeline):
        rows.append(row)
    return rows


def _distance_of(row: dict[str, Any]) -> float:
    return float(row.get("distanceMeters") or 0.0)


def _passes_filters(
    document: dict[str, Any],
    *,
    counts: dict[str, int],
    types: dict[str, dict[str, int]],
    min_available: int,
    max_price: float | None,
    want_ev: bool,
    want_covered: bool,
    want_accessible: bool,
    open_now: bool,
    space_type: str | None,
) -> bool:
    """Whether one candidate survives every filter.

    `maxPrice` and `spaceType` are the two with a rule worth spelling out:

    * A facility with no `hourlyRate` is *not* a cheap facility. It is a
      facility whose price has not been published, so it is excluded from a
      "under Rs X" search rather than counted as zero.
    * DOC section 22's "parking type" means a space the driver can actually
      take, so the type has to have at least one `AVAILABLE` space. A garage
      full of blocked EV bays is not an EV facility as far as the search is
      concerned.
    """
    if min_available > 0 and counts.get("available", 0) < min_available:
        return False

    if max_price is not None:
        hourly_rate = document.get("hourlyRate")
        if hourly_rate is None or hourly_rate > max_price:
            return False

    amenities = set(document.get("amenities") or [])
    if want_ev and Amenity.EV_CHARGING.value not in amenities:
        return False
    if want_covered and Amenity.COVERED.value not in amenities:
        return False
    if want_accessible and Amenity.ACCESSIBLE.value not in amenities:
        return False

    if open_now and not is_open_now(document):
        return False

    if space_type is not None:
        available_of_type = types.get(space_type, {}).get(SpaceStatus.AVAILABLE.value, 0)
        if available_of_type < 1:
            return False

    return True


def _sort_rows(
    rows: list[dict[str, Any]],
    *,
    sort_by: ParkingSortBy,
    counts_by_facility: dict[str, dict[str, int]],
) -> list[dict[str, Any]]:
    """Order the candidates for the requested sort.

    Facilities with nothing to compare on (no published rate, no rating) sort
    last rather than first, and every tie is broken by distance and then by
    name so the order is stable between requests.
    """

    def rate(row: dict[str, Any]) -> float:
        value = row.get("hourlyRate")
        return float(value) if value is not None else float("inf")

    def rating_desc(row: dict[str, Any]) -> float:
        value = row.get("rating")
        return -float(value) if value is not None else float("inf")

    def available_desc(row: dict[str, Any]) -> int:
        return -counts_by_facility.get(str(row["_id"]), {}).get("available", 0)

    def distance(row: dict[str, Any]) -> float:
        return _distance_of(row)

    def name(row: dict[str, Any]) -> str:
        return str(row.get("name") or "")

    if sort_by == "price":
        primary = rate
    elif sort_by == "availability":
        primary = available_desc
    elif sort_by == "rating":
        primary = rating_desc
    else:
        primary = distance

    return sorted(rows, key=lambda row: (primary(row), distance(row), name(row)))


async def nearby(
    db: AsyncDatabase,
    *,
    lat: float,
    lng: float,
    radius_meters: float,
    min_available: int = 0,
    max_price: float | None = None,
    want_ev: bool = False,
    want_covered: bool = False,
    want_accessible: bool = False,
    open_now: bool = False,
    space_type: str | None = None,
    sort_by: ParkingSortBy = "distance",
    page: int = 1,
    limit: int = 20,
) -> tuple[list[dict[str, Any]], int]:
    """Facilities near a point, as `(rows, total)`.

    The radius is a *tier boundary*, not a filter: facilities inside it come
    first, then the nearest facilities outside it, and the two tiers are
    concatenated before the page is sliced. So a driver always gets a full page
    while anything is active, with the closest facilities on top.

    `rows` are the public projection of one page of results, each carrying the
    distance from the search point and the moment its availability was read.
    `total` is the number of facilities that matched every filter across both
    tiers, so the client can show "showing 20 of 87" and page through them.
    """
    candidates = await _candidates(db, lat=lat, lng=lng)
    if not candidates:
        return [], 0

    facility_ids = [row["_id"] for row in candidates]
    counts_by_facility = await space_counts(db, facility_ids)
    types_by_facility = await space_type_counts(db, facility_ids)

    matched = [
        row
        for row in candidates
        if _passes_filters(
            row,
            counts=counts_by_facility.get(str(row["_id"]), {}),
            types=types_by_facility.get(str(row["_id"]), {}),
            min_available=min_available,
            max_price=max_price,
            want_ev=want_ev,
            want_covered=want_covered,
            want_accessible=want_accessible,
            open_now=open_now,
            space_type=space_type,
        )
    ]

    # Inside the radius first, then the nearest facilities beyond it. Each tier
    # is ordered by the caller's sort, so "closest on top" holds even when the
    # sort is price or rating.
    inside = [row for row in matched if _distance_of(row) <= radius_meters]
    outside = [row for row in matched if _distance_of(row) > radius_meters]
    ordered = _sort_rows(
        inside, sort_by=sort_by, counts_by_facility=counts_by_facility
    ) + _sort_rows(outside, sort_by=sort_by, counts_by_facility=counts_by_facility)

    total = len(ordered)
    start = (page - 1) * limit
    if start >= total:
        return [], total
    page_rows = ordered[start : start + limit]

    operator_by_id = await operator_names(db, page_rows)
    measured_at = utcnow()
    results: list[dict[str, Any]] = []
    for row in page_rows:
        facility_id = str(row["_id"])
        public = public_facility(
            row,
            operator_name=operator_by_id.get(str(row.get("operatorId"))),
            space_counts=counts_by_facility.get(facility_id, {}),
        )
        public["distanceMeters"] = round(_distance_of(row), 1)
        public["availabilityAsOf"] = measured_at
        public["spaceTypes"] = sorted(types_by_facility.get(facility_id, {}).keys())
        results.append(public)

    return results, total


async def detail(
    db: AsyncDatabase, *, facility_id: str
) -> dict[str, Any]:
    """One facility, with its live availability and the kinds of space it holds.

    The driver-facing twin of `GET /facilities/{id}`: same document, but
    readable by a driver and carrying the availability numbers a search row
    would have shown.
    """
    object_id = to_object_id(facility_id)
    if object_id is None:
        raise NotFoundError(FACILITY_NOT_FOUND)

    document = await db[FACILITIES_COLLECTION].find_one({"_id": object_id})
    if document is None:
        raise NotFoundError(FACILITY_NOT_FOUND)

    facility_ids = [document["_id"]]
    counts_by_facility = await space_counts(db, facility_ids)
    types_by_facility = await space_type_counts(db, facility_ids)

    operator_by_id = await operator_names(db, [document])
    public = public_facility(
        document,
        operator_name=operator_by_id.get(str(document.get("operatorId"))),
        space_counts=counts_by_facility.get(str(document["_id"]), {}),
    )
    public["distanceMeters"] = None
    public["availabilityAsOf"] = utcnow()
    public["spaceTypes"] = sorted(types_by_facility.get(str(document["_id"]), {}).keys())
    return public


async def availability(
    db: AsyncDatabase, *, facility_id: str
) -> dict[str, Any]:
    """The facility's per-status space tally, and when it was read."""
    object_id = to_object_id(facility_id)
    if object_id is None:
        raise NotFoundError(FACILITY_NOT_FOUND)

    document = await db[FACILITIES_COLLECTION].find_one({"_id": object_id}, {"_id": 1})
    if document is None:
        raise NotFoundError(FACILITY_NOT_FOUND)

    counts_by_facility = await space_counts(db, [document["_id"]])
    counts = counts_by_facility.get(str(document["_id"]), {})
    return {
        "facilityId": facility_id,
        "spaceCounts": {
            "total": int(counts.get("total", 0)),
            "available": int(counts.get("available", 0)),
            "occupied": int(counts.get("occupied", 0)),
            "reserved": int(counts.get("reserved", 0)),
            "maintenance": int(counts.get("maintenance", 0)),
            "blocked": int(counts.get("blocked", 0)),
        },
        "availabilityAsOf": utcnow(),
    }


async def spaces(db: AsyncDatabase, *, facility_id: str) -> tuple[list[dict[str, Any]], Any]:
    """A facility's spaces, for the driver's layout grid.

    The `facilities` group refuses drivers, and its spaces route inherits that
    restriction, so the discovery group offers its own read of the same
    collection: every space of the facility, with the floor and zone names
    denormalised onto it, ordered so the grid reads floor by floor.

    Returns the rows and the moment they were read.
    """
    object_id = to_object_id(facility_id)
    if object_id is None:
        raise NotFoundError(FACILITY_NOT_FOUND)

    document = await db[FACILITIES_COLLECTION].find_one({"_id": object_id}, {"_id": 1})
    if document is None:
        raise NotFoundError(FACILITY_NOT_FOUND)

    rows = (
        await db[SPACES_COLLECTION]
        .find({"facilityId": object_id})
        .sort([("floorName", 1), ("zoneName", 1), ("spaceNumber", 1), ("_id", 1)])
        .to_list()
    )
    return rows, utcnow()


__all__ = [
    "availability",
    "detail",
    "nearby",
    "normalise_sort",
    "spaces",
]
