import csv
import gzip
import json
import re
from collections import Counter

import pytest
from django.core.management import call_command

from apps.geography.models import Lga, PollingUnit, Ward
from apps.geography.polling_units import (
    COLUMNS,
    DATA_FILE,
    PollingUnitDataError,
    load_polling_units,
)
from apps.geography.seed import DATA_FILE as GEOGRAPHY_FILE

CODE = re.compile(r"^\d{2}-\d{2}-\d{2}-\d{3}$")


def write_seed(path, rows):
    with gzip.open(path, "wt", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(COLUMNS)
        writer.writerows(rows)
    return path


ZARIA = ["KD-ZARIA", "18-23", "18-23-01", 'KWARBAI "A"']


# --- the shipped data file (no database) ----------------------------------


def test_shipped_file_covers_every_inec_polling_unit():
    with gzip.open(DATA_FILE, "rt", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    canonical_lgas = {
        lga["code"] for lga in json.loads(GEOGRAPHY_FILE.read_text(encoding="utf-8"))["lgas"]
    }

    assert len(rows) == 176_846
    assert len({r["pu_inec"] for r in rows}) == 176_846
    assert len({r["ward_inec"] for r in rows}) == 8_809
    assert {r["lga"] for r in rows} == canonical_lgas
    assert len({r["lga_inec"] for r in rows}) == 774
    assert all(CODE.match(r["pu_inec"]) for r in rows)
    assert all(r["pu_inec"].startswith(r["ward_inec"] + "-") for r in rows)
    assert all(r["pu_name"] for r in rows)

    per_state = Counter(r["lga"].split("-")[0] for r in rows)
    assert per_state["LA"] == 13_325
    assert per_state["KD"] == 8_012


# --- loader ---------------------------------------------------------------


@pytest.mark.django_db
def test_zaria_matches_independent_inec_list():
    counts = load_polling_units(lga_codes=["KD-ZARIA"])
    assert counts == {"lgas": 1, "wards": 13, "polling_units": 584, "not_in_file": 0}
    assert Lga.objects.get(code="KD-ZARIA").inec_code == "18-23"
    ward = Ward.objects.get(code="18-23-01")
    assert ward.name == 'KWARBAI "A"'
    assert ward.polling_units.count() == 51
    assert PollingUnit.objects.get(code="18-23-01-001").ward.lga.state.code == "KD"


@pytest.mark.django_db
def test_reload_refreshes_names_but_keeps_local_fields(tmp_path):
    path = write_seed(tmp_path / "pus.csv.gz", [[*ZARIA, "18-23-01-001", "OLD NAME", ""]])
    load_polling_units(path)
    PollingUnit.objects.filter(code="18-23-01-001").update(
        registered_voters=742, latitude="11.08", longitude="7.71"
    )

    write_seed(path, [[*ZARIA, "18-23-01-001", "NEW NAME", "PRI. SCH."]])
    load_polling_units(path)
    pu = PollingUnit.objects.get(code="18-23-01-001")
    assert (pu.name, pu.location) == ("NEW NAME", "PRI. SCH.")
    assert pu.registered_voters == 742
    assert str(pu.latitude) == "11.080000"


@pytest.mark.django_db
def test_units_missing_from_new_file_are_reported_not_deleted(tmp_path):
    path = tmp_path / "pus.csv.gz"
    write_seed(
        path,
        [[*ZARIA, "18-23-01-001", "A", ""], [*ZARIA, "18-23-01-002", "B", ""]],
    )
    load_polling_units(path)
    write_seed(path, [[*ZARIA, "18-23-01-001", "A", ""]])
    assert load_polling_units(path)["not_in_file"] == 1
    assert PollingUnit.objects.filter(code="18-23-01-002").exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "rows, message",
    [
        ([["XX-NOWHERE", "99-99", "99-99-01", "W", "99-99-01-001", "P", ""]], "Unknown LGA"),
        (
            [[*ZARIA, "18-23-01-001", "A", ""], [*ZARIA, "18-23-01-001", "B", ""]],
            "Duplicate polling unit",
        ),
    ],
)
def test_loader_rejects_bad_data(tmp_path, rows, message):
    path = write_seed(tmp_path / "pus.csv.gz", rows)
    with pytest.raises(PollingUnitDataError, match=message):
        load_polling_units(path)
    assert not PollingUnit.objects.exists()  # all or nothing


@pytest.mark.django_db
def test_loader_rejects_unexpected_columns(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("state,lga\nKD,ZARIA\n", encoding="utf-8")
    with pytest.raises(PollingUnitDataError, match="Unexpected columns"):
        load_polling_units(path)


@pytest.mark.django_db
def test_command_and_api(auth_client):
    call_command("load_polling_units", "--lga", "KD-ZARIA")
    unit = auth_client.get("/api/v1/geography/polling-units/18-23-01-001/").json()
    assert unit["name"] == "KWARBAI I/ PRI. SCH. MADARKACI"
    assert unit["ward"] == "18-23-01"
    wards = auth_client.get("/api/v1/geography/wards/", {"lga__code": "KD-ZARIA"}).json()
    assert wards["count"] == 13
    lga = auth_client.get("/api/v1/geography/lgas/KD-ZARIA/").json()
    assert lga["inec_code"] == "18-23"
