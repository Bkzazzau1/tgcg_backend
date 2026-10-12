"""Check coordinates collected by inec_locator.py before importing them.

Each polling unit's point must:
- belong to a unit in the INEC register (data/inec_polling_units.csv.gz);
- lie inside the unit's LGA, allowing 1 km for boundary detail. A point
  further out but inside a neighbouring LGA of the same state, within 5 km
  of its own, is kept and flagged for a coordinator to check: border towns
  are often drawn on the wrong side of the line;
- not be one spot shared by units from three or more wards, which marks a
  town-centre placeholder rather than a building.

Points outside their ward (where GRID3 ward boundaries exist) are reported but
kept, because GRID3's operational wards do not always follow INEC's lines.

Merges the results into the file load_inec_locations imports (default
apps/geography/data/inec_locations.csv.gz), keeping units collected earlier,
and prints a summary.

Usage (shapely is only needed for this check):
    uv run --with shapely python scripts/check_inec_locations.py \\
        --locations inec_locator.csv --geo ../TheGameChangerGroup/assets/geo
"""

import argparse
import csv
import gzip
import io
import json
from collections import Counter, defaultdict
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
REGISTER = BACKEND / "apps" / "geography" / "data" / "inec_polling_units.csv.gz"
OUTPUT = BACKEND / "apps" / "geography" / "data" / "inec_locations.csv.gz"
GEOGRAPHY = BACKEND / "apps" / "geography" / "data" / "nigeria_geography.json"
LGA_MARGIN_DEGREES = 0.009  # about 1 km
# Further out, a point inside a neighbouring LGA of the same state is kept but
# flagged: border towns are often drawn on the wrong side of the line.
NEIGHBOUR_MARGIN_DEGREES = 0.045  # about 5 km
SHARED_SPOT_WARDS = 3
COLUMNS = ["code", "latitude", "longitude", "qa", "fetched_at"]


def shape(rings):
    # shapely is imported here so the rest of this file loads without it (tests).
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    polygons = [Polygon(list(zip(ring[0::2], ring[1::2], strict=True))) for ring in rings]
    return unary_union([p if p.is_valid else p.buffer(0) for p in polygons])


def decimals(text):
    return len(text.split(".", 1)[1]) if "." in text else 0


def read_collected(path):
    """Collected rows by code (the last row for a code wins).

    A half-written last line is ignored, so the file can be checked while the
    collector is still adding to it.
    """
    text = Path(path).read_text(encoding="utf-8")
    text = text[: text.rfind("\n") + 1]
    return {row["code"]: row for row in csv.DictReader(io.StringIO(text)) if row.get("status")}


def complete_lgas(collected, units):
    """LGAs whose every polling unit in the register has been collected."""
    by_lga = defaultdict(set)
    for code, unit in units.items():
        by_lga[unit["lga"]].add(code)
    touched = {units[code]["lga"] for code in collected if code in units}
    return {lga for lga in touched if by_lga[lga] <= collected.keys()}


def check(locations, register, geo, complete_only=False):
    from shapely.geometry import Point

    units = {}
    with gzip.open(register, "rt", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            units[row["pu_inec"]] = row

    collected = read_collected(locations)
    if complete_only:
        done = complete_lgas(collected, units)
        collected = {
            code: row
            for code, row in collected.items()
            if code in units and units[code]["lga"] in done
        }

    lga_shapes, ward_shapes = {}, {}

    def lga_shape(lga):
        if lga not in lga_shapes:
            data = json.loads(Path(geo, "lga", f"{lga.split('-')[0]}.json").read_text("utf-8"))
            lga_shapes.update({code: v["rings"] for code, v in data["lgas"].items()})
        rings = lga_shapes.get(lga)
        if isinstance(rings, list):
            lga_shapes[lga] = shape(rings)
        return lga_shapes.get(lga)

    def in_neighbouring_lga(lga, point):
        """The point lies inside another LGA of the same state."""
        state = lga.split("-")[0]
        lga_shape(lga)  # loads the state's LGAs
        return any(
            lga_shape(code).contains(point)
            for code in list(lga_shapes)
            if code.split("-")[0] == state and code != lga
        )

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
            beyond = lga.distance(point) if lga is not None else 0
            if beyond > LGA_MARGIN_DEGREES:
                nearby = beyond <= NEIGHBOUR_MARGIN_DEGREES and in_neighbouring_lga(
                    unit["lga"], point
                )
                out["qa"] = "ok_neighbouring_lga" if nearby else "outside_lga"
                out["ward"] = unit["ward_inec"]
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


def coverage_table(rows, units, names):
    """A Markdown table of what the data file holds, one line per LGA."""
    totals = defaultdict(Counter)
    for row in rows:
        unit = units.get(row["code"])
        if unit:
            totals[unit["lga"]][row["qa"]] += 1
    register = Counter(unit["lga"] for unit in units.values())
    lines = [
        "| LGA | Units | Located | Outside own ward | In next LGA | No INEC location | Rejected |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    grand = Counter()
    for lga in sorted(totals, key=lambda code: names.get(code, code)):
        qa = totals[lga]
        located = qa["ok"] + qa["ok_outside_ward"] + qa["ok_neighbouring_lga"]
        cells = {
            "units": register[lga],
            "located": located,
            "outside_ward": qa["ok_outside_ward"],
            "next_lga": qa["ok_neighbouring_lga"],
            "missing": qa["no_location"],
            "rejected": sum(qa.values()) - located - qa["no_location"],
        }
        grand.update(cells)
        lines.append(
            f"| {names.get(lga, lga)} | {cells['units']:,} | {cells['located']:,} | "
            f"{cells['outside_ward']:,} | {cells['next_lga']:,} | {cells['missing']:,} | "
            f"{cells['rejected']:,} |"
        )
    lines.append(
        f"| **Total** | **{grand['units']:,}** | **{grand['located']:,}** | "
        f"**{grand['outside_ward']:,}** | **{grand['next_lga']:,}** | **{grand['missing']:,}** | "
        f"**{grand['rejected']:,}** |"
    )
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--locations", required=True, help="CSV written by inec_locator.py")
    parser.add_argument("--geo", required=True, help="the Flutter app's assets/geo folder")
    parser.add_argument("--register", default=str(REGISTER))
    parser.add_argument("--out", default=str(OUTPUT))
    parser.add_argument(
        "--complete-only",
        action="store_true",
        help="only take LGAs whose units have all been collected (safe during a run)",
    )
    args = parser.parse_args()

    results, units, spots = check(args.locations, args.register, args.geo, args.complete_only)
    # Merge into the existing file so LGAs collected earlier are kept.
    merged = {}
    if Path(args.out).exists():
        with gzip.open(args.out, "rt", encoding="utf-8", newline="") as handle:
            merged = {row["code"]: row for row in csv.DictReader(handle)}
    earlier = len(merged.keys() - {out["code"] for out in results})
    merged.update({out["code"]: out for out in results})
    with gzip.open(args.out, "wt", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(row for _, row in sorted(merged.items()))

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
    print(f"Wrote {args.out}: {len(merged):,} units, {earlier:,} of them from earlier collections")
    geography = json.loads(GEOGRAPHY.read_text(encoding="utf-8"))
    names = {lga["code"]: lga["name"] for lga in geography["lgas"]}
    print("\nCoverage of the data file (for apps/geography/data/README.md):\n")
    print(coverage_table(merged.values(), units, names))


if __name__ == "__main__":
    main()
