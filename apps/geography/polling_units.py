"""Load INEC wards and polling units onto the canonical LGAs.

Source file: data/inec_polling_units.csv.gz (see data/README.md for
provenance). Re-running is safe: names and locations are refreshed, while
registered voters and coordinates entered locally are never overwritten.
Units missing from a newer file are reported, not deleted, because results
may reference them.
"""

import csv
import gzip
from pathlib import Path

from django.db import transaction

from .models import Lga, PollingUnit, Ward

DATA_FILE = Path(__file__).resolve().parent / "data" / "inec_polling_units.csv.gz"
COLUMNS = ["lga", "lga_inec", "ward_inec", "ward_name", "pu_inec", "pu_name", "location"]
BATCH = 5000


class PollingUnitDataError(ValueError):
    pass


def _rows(path):
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != COLUMNS:
            raise PollingUnitDataError(f"Unexpected columns: {reader.fieldnames}")
        yield from reader


@transaction.atomic
def load_polling_units(path=DATA_FILE, *, lga_codes=None) -> dict:
    """Upsert wards and polling units. `lga_codes` limits loading to some LGAs."""
    lgas = {lga.code: lga for lga in Lga.objects.all()}
    wards, units, lga_inec = {}, {}, {}

    for row in _rows(path):
        if lga_codes is not None and row["lga"] not in lga_codes:
            continue
        lga = lgas.get(row["lga"])
        if lga is None:
            raise PollingUnitDataError(f"Unknown LGA {row['lga']!r}")
        known = lga_inec.setdefault(lga.code, row["lga_inec"])
        if known != row["lga_inec"]:
            raise PollingUnitDataError(f"LGA {lga.code} has two INEC codes")
        wards.setdefault(row["ward_inec"], (row["ward_name"], lga))
        if row["pu_inec"] in units:
            raise PollingUnitDataError(f"Duplicate polling unit {row['pu_inec']}")
        units[row["pu_inec"]] = (row["pu_name"], row["location"], row["ward_inec"])

    for code, inec in lga_inec.items():
        lga = lgas[code]
        if lga.inec_code != inec:
            lga.inec_code = inec
            lga.save(update_fields=["inec_code"])

    Ward.objects.bulk_create(
        [Ward(code=code, name=name, lga=lga) for code, (name, lga) in wards.items()],
        batch_size=BATCH,
        update_conflicts=True,
        unique_fields=["code"],
        update_fields=["name", "lga"],
    )
    ward_ids = dict(Ward.objects.values_list("code", "pk"))

    pending = []
    for code, (name, location, ward_code) in units.items():
        pending.append(
            PollingUnit(code=code, name=name, location=location, ward_id=ward_ids[ward_code])
        )
        if len(pending) >= BATCH:
            _upsert(pending)
            pending = []
    _upsert(pending)

    scope = PollingUnit.objects.all()
    if lga_codes is not None:
        scope = scope.filter(ward__lga__code__in=lga_codes)
    # Compared in Python: an IN list of 176k codes exceeds database parameter limits.
    stale = sum(1 for code in scope.values_list("code", flat=True).iterator() if code not in units)
    return {
        "lgas": len(lga_inec),
        "wards": len(wards),
        "polling_units": len(units),
        "not_in_file": stale,
    }


def _upsert(objs):
    if objs:
        PollingUnit.objects.bulk_create(
            objs,
            update_conflicts=True,
            unique_fields=["code"],
            # registered_voters, latitude and longitude are deliberately kept.
            update_fields=["name", "location", "ward"],
        )
