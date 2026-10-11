"""Check coordinates collected by inec_locator.py before importing them.

Each polling unit's point must:
- belong to a unit in the INEC register (data/inec_polling_units.csv.gz);
- lie inside the unit's LGA, allowing 1 km for boundary detail;
- not be one spot shared by units from three or more wards, which marks a
  town-centre placeholder rather than a building.

Points outside their ward (where GRID3 ward boundaries exist) are reported but
kept, because GRID3's operational wards do not always follow INEC's lines.

Writes the file load_inec_locations imports (default
apps/geography/data/inec_locations.csv.gz) and prints a summary.

Usage (shapely is only needed for this check):
    uv run --with shapely python scripts/check_inec_locations.py \\
        --locations inec_locator.csv --geo ../TheGameChangerGroup/assets/geo
"""

import argparse
import csv
import gzip
import json
from collections import Counter, defaultdict
from pathlib import Path

from shapely.geometry import Point, Polygon
from shapely.ops import unary_union

BACKEND = Path(__file__).resolve().parent.parent
REGISTER = BACKEND / "apps" / "geography" / "data" / "inec_polling_units.csv.gz"
OUTPUT = BACKEND / "apps" / "geography" / "data" / "inec_locations.csv.gz"
LGA_MARGIN_DEGREES = 0.009  # about 1 km
SHARED_SPOT_WARDS = 3
COLUMNS = ["code", "latitude", "longitude", "qa", "fetched_at"]


def shape(rings):
    polygons = [Polygon(list(zip(ring[0::2], ring[1::2], strict=True))) for ring in rings]
    return unary_union([p if p.is_valid else p.buffer(0) for p in polygons])


def decimals(text):
    return len(text.split(".", 1)[1]) if "." in text else 0


def check(locations, register, geo):
    units = {}
    with gzip.open(register, "rt", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            units[row["pu_inec"]] = row

    collected = {}
    with open(locations, encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            collected[row["code"]] = row  # the last row for a code wins

    lga_shapes, ward_shapes = {}, {}

    def lga_shape(lga):
        if lga not in lga_shapes:
            data = json.loads(Path(geo, "lga", f"{lga.split('-')[0]}.json").read_text("utf-8"))
            lga_shapes.update({code: v["rings"] for code, v in data["lgas"].items()})
        rings = lga_shapes.get(lga)
        if isinstance(rings, list):
            lga_shapes[lga] = shape(rings)
        return lga_shapes.get(lga)

    def ward_shape(lga, ward):
        if lga not in ward_shapes:
            path = Path(geo, "ward", f"{lga}.json")
            data = json.loads(path.read_text("utf-8")) if path.exists() else {"wards": {}}
            ward_shapes[lga] = {
                code: shape(v["rings"]) for code, v in data["wards"].items() if "rings" in v
            }
        return ward_shapes[lga].get(ward)

    results = []
    for code, row in sorted(collected.items()):
        out = {
            "code": code,
            "latitude": row["latitude"],
            "longitude": row["longitude"],
            "fetched_at": row["fetched_at"],
        }
        unit = units.get(code)
        if row["status"] != "ok":
            out["qa"] = row["status"]
        elif unit is None:
            out["qa"] = "not_in_register"
        else:
            point = Point(float(row["longitude"]), float(row["latitude"]))
            lga = lga_shape(unit["lga"])
            if lga is not None and lga.distance(point) > LGA_MARGIN_DEGREES:
                out["qa"] = "outside_lga"
            else:
                ward = ward_shape(unit["lga"], unit["ward_inec"])
                out["qa"] = "ok_outside_ward" if ward and not ward.contains(point) else "ok"
                out["ward"] = unit["ward_inec"]
        results.append(out)

    # One spot claimed by units of several wards is a placeholder, not a building.
    spots = defaultdict(list)
    for out in results:
        if out["qa"].startswith("ok"):
            spots[(round(float(out["latitude"]), 5), round(float(out["longitude"]), 5))].append(out)
    for group in spots.values():
        if len({out["ward"] for out in group}) >= SHARED_SPOT_WARDS:
            for out in group:
                out["qa"] = "shared_spot"
    return results, units, spots


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--locations", required=True, help="CSV written by inec_locator.py")
    parser.add_argument("--geo", required=True, help="the Flutter app's assets/geo folder")
    parser.add_argument("--register", default=str(REGISTER))
    parser.add_argument("--out", default=str(OUTPUT))
    args = parser.parse_args()

    results, units, spots = check(args.locations, args.register, args.geo)
    with gzip.open(args.out, "wt", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)

    qa = Counter(out["qa"] for out in results)
    located = [out for out in results if out["latitude"]]
    print(f"Units collected: {len(results):,} of {len(units):,} in the register")
    for key, value in qa.most_common():
        print(f"  {key:18} {value:,}")
    print(
        "Latitude decimal places:",
        dict(sorted(Counter(decimals(o["latitude"]) for o in located).items())),
    )
    print(
        "Longitude decimal places:",
        dict(sorted(Counter(decimals(o["longitude"]) for o in located).items())),
    )
    sizes = Counter(len(group) for group in spots.values())
    print("Units per spot:", dict(sorted(sizes.items())))
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
