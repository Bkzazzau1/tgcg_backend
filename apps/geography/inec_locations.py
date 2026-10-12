"""Load polling unit locations from INEC's Polling Unit Locator.

Source file: data/inec_locations.csv.gz, collected with scripts/inec_locator.py
and checked with scripts/check_inec_locations.py (see data/README.md).

INEC's point is treated as a first pin: the unit gets directions straight
away, one coordinator pin nearby confirms it, and a pin further away raises a
conflict. Locations from the field (coordinator pins and confirmed check-ins)
are never overwritten, and neither are units a coordinator has already checked
or reported as moved or missing. Re-running is safe.
"""

import csv
import gzip
import math
from collections import Counter
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from django.db import transaction

from .gps import EARTH_RADIUS_M
from .models import PollingUnit

DATA_FILE = Path(__file__).resolve().parent / "data" / "inec_locations.csv.gz"
COLUMNS = ["code", "latitude", "longitude", "qa", "fetched_at"]
# Quality checks that passed (see scripts/check_inec_locations.py).
ACCEPTED = {"ok", "ok_outside_ward", "ok_neighbouring_lga"}
# Points just inside a neighbouring LGA of the same state: usually a border
# town drawn on the wrong side of the line, so kept, but worth a look.
NEIGHBOURING_LGA_NOTE = (
    "INEC's point lies just inside the neighbouring LGA on the map. Check it on arrival."
)
FIELD_SOURCES = {"coordinator", "checkIn"}
SIX_PLACES = Decimal("0.000001")
# The locator stores latitudes to 4 or 8 decimal places and drops trailing
# zeros, so 11.064 is really 11.0640. In Zaria, 157 latitudes had 4 places,
# 24 had 3 and 2 had 2: the 10:1 pattern trailing zeros produce.
MIN_PLACES = 4
BATCH = 2000


class InecLocationDataError(ValueError):
    pass


def rounding_margin_m(latitude: str, longitude: str) -> float | None:
    """How far off a coordinate can be just from how INEC rounded it.

    Four decimal places of latitude is up to about 6 m. Returns None when the
    rounding is under a metre.
    """

    def places(text):
        written = len(text.split(".", 1)[1]) if "." in text else 0
        return max(written, MIN_PLACES)

    metres_per_degree = math.pi * EARTH_RADIUS_M / 180
    lat_error = metres_per_degree * 10 ** -places(latitude) / 2
    lng_error = (
        metres_per_degree * math.cos(math.radians(float(latitude))) * 10 ** -places(longitude) / 2
    )
    margin = math.hypot(lat_error, lng_error)
    return round(margin, 1) if margin >= 1 else None


def _rows(path):
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = set(COLUMNS) - set(reader.fieldnames or [])
        if missing:
            raise InecLocationDataError(f"Missing columns: {sorted(missing)}")
        yield from reader


def _decimal(text):
    return Decimal(text).quantize(SIX_PLACES, rounding=ROUND_HALF_UP)


@transaction.atomic
def load_inec_locations(path=DATA_FILE) -> Counter:
    """Apply INEC's locations to polling units. Returns counts of what happened."""
    counts = Counter()
    rows = {}
    for row in _rows(path):
        if row["qa"] in ACCEPTED:
            rows[row["code"]] = row  # the last row for a code wins
        else:
            counts[f"rejected_{row['qa']}"] += 1

    changed = []
    seen = set()
    for unit in PollingUnit.objects.only(
        "id", "code", "latitude", "longitude", "location_source", "location_note", "mapping_status"
    ).iterator(chunk_size=5000):
        row = rows.get(unit.code)
        if row is None:
            continue
        seen.add(unit.code)
        if unit.location_source in FIELD_SOURCES:
            counts["kept_field_location"] += 1
            continue
        if unit.mapping_status in ("confirmed", "conflict"):
            counts["kept_checked_by_coordinator"] += 1
            continue
        if unit.mapping_status == "problem":
            counts["kept_problem_report"] += 1
            continue
        latitude, longitude = _decimal(row["latitude"]), _decimal(row["longitude"])
        note = NEIGHBOURING_LGA_NOTE if row["qa"] == "ok_neighbouring_lga" else ""
        if (
            unit.location_source == "inec"
            and unit.latitude == latitude
            and unit.longitude == longitude
            and unit.location_note == note
        ):
            counts["unchanged"] += 1
            continue
        counts["updated" if unit.location_source == "inec" else "imported"] += 1
        unit.latitude, unit.longitude = latitude, longitude
        unit.location_source = "inec"
        unit.location_note = note
        unit.location_accuracy_m = rounding_margin_m(row["latitude"], row["longitude"])
        unit.location_confirmed_by = None
        unit.location_confirmed_at = (
            datetime.fromisoformat(row["fetched_at"]) if row["fetched_at"] else None
        )
        unit.mapping_status = "pinned"
        changed.append(unit)

    PollingUnit.objects.bulk_update(
        changed,
        [
            "latitude",
            "longitude",
            "location_source",
            "location_note",
            "location_accuracy_m",
            "location_confirmed_by",
            "location_confirmed_at",
            "mapping_status",
        ],
        batch_size=BATCH,
    )
    counts["unknown_code"] = len(rows.keys() - seen)
    return counts
