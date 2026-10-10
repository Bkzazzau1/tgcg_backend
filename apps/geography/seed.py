"""Load the canonical geography exported from the Flutter client.

The data file is produced in the Flutter repo by
`dart run tool/export_geography.dart`. Loading is idempotent: existing rows
are updated in place, so it is safe to re-run after the catalogue changes.
"""

import json
from pathlib import Path

DATA_FILE = Path(__file__).resolve().parent / "data" / "nigeria_geography.json"


def load_canonical_geography(apps=None, data_file=DATA_FILE):
    """Upsert zones, states, senatorial districts and LGAs.

    `apps` is the historical app registry when called from a migration.
    Returns a dict of row counts per level.
    """
    if apps is None:
        from django.apps import apps as global_apps

        apps = global_apps
    Zone = apps.get_model("geography", "Zone")
    State = apps.get_model("geography", "State")
    SenatorialDistrict = apps.get_model("geography", "SenatorialDistrict")
    Lga = apps.get_model("geography", "Lga")

    data = json.loads(Path(data_file).read_text(encoding="utf-8"))

    zones = {}
    for row in data["zones"]:
        zones[row["code"]], _ = Zone.objects.update_or_create(
            code=row["code"], defaults={"name": row["name"]}
        )

    states = {}
    for row in data["states"]:
        states[row["code"]], _ = State.objects.update_or_create(
            code=row["code"],
            defaults={
                "name": row["name"],
                "zone": zones[row["zone"]],
                "is_fct": row["is_fct"],
            },
        )

    districts = {}
    for row in data["senatorial_districts"]:
        districts[row["code"]], _ = SenatorialDistrict.objects.update_or_create(
            code=row["code"],
            defaults={"name": row["name"], "state": states[row["state"]]},
        )

    for row in data["lgas"]:
        Lga.objects.update_or_create(
            code=row["code"],
            defaults={
                "slug": row["slug"],
                "name": row["name"],
                "state": states[row["state"]],
                "senatorial_district": districts[row["senatorial_district"]],
            },
        )

    return {
        "zones": len(data["zones"]),
        "states": len(data["states"]),
        "senatorial_districts": len(data["senatorial_districts"]),
        "lgas": len(data["lgas"]),
    }
