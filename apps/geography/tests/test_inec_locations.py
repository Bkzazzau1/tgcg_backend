import csv
from decimal import Decimal

import pytest
from django.core.management import call_command

from apps.geography.inec_locations import COLUMNS, load_inec_locations, rounding_margin_m
from apps.geography.models import PollingUnit
from apps.geography.polling_units import load_polling_units

pytestmark = pytest.mark.django_db

FETCHED = "2026-10-11T07:00:00+00:00"


@pytest.fixture(autouse=True)
def zaria():
    load_polling_units(lga_codes=["KD-ZARIA"])


def write(path, rows):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, COLUMNS)
        writer.writeheader()
        for code, latitude, longitude, qa in rows:
            writer.writerow(
                {
                    "code": code,
                    "latitude": latitude,
                    "longitude": longitude,
                    "qa": qa,
                    "fetched_at": FETCHED,
                }
            )
    return path


def test_rounding_margin():
    # Four decimal places of latitude can be up to ~6 m off.
    assert rounding_margin_m("11.0642", "7.716035636") == pytest.approx(5.6, abs=0.1)
    # 11.064 is 11.0640 with its trailing zero dropped, not a coarser reading.
    assert rounding_margin_m("11.064", "7.716162489") == pytest.approx(5.6, abs=0.1)
    assert rounding_margin_m("11.05388889", "7.712944") is None


def test_import_treats_inec_as_a_first_pin_and_keeps_field_data(tmp_path):
    PollingUnit.objects.filter(code="18-23-01-004").update(
        latitude="11.070000",
        longitude="7.710000",
        location_source="coordinator",
        mapping_status="pinned",
    )
    PollingUnit.objects.filter(code="18-23-01-005").update(mapping_status="problem")
    path = write(
        tmp_path / "inec.csv",
        [
            ("18-23-01-001", "11.0642", "7.716035636", "ok"),
            ("18-23-01-002", "11.064", "7.716162489", "ok_outside_ward"),
            ("18-23-01-003", "12.5", "4.2", "outside_lga"),
            ("18-23-01-004", "11.0645", "7.710246112", "ok"),
            ("18-23-01-005", "11.06", "7.71", "ok"),
            ("99-99-99-999", "11.06", "7.71", "ok"),
        ],
    )

    counts = load_inec_locations(path)
    assert counts["imported"] == 2
    assert counts["rejected_outside_lga"] == 1
    assert counts["kept_field_location"] == 1
    assert counts["kept_problem_report"] == 1
    assert counts["unknown_code"] == 1

    unit = PollingUnit.objects.get(code="18-23-01-001")
    assert unit.location_source == "inec"
    assert unit.mapping_status == "pinned"
    assert unit.latitude == Decimal("11.064200")
    assert unit.longitude == Decimal("7.716036")  # stored to six places
    assert unit.location_accuracy_m == pytest.approx(5.6, abs=0.1)
    assert unit.location_confirmed_by is None
    assert PollingUnit.objects.get(code="18-23-01-002").location_accuracy_m == pytest.approx(
        5.6, abs=0.1
    )
    assert PollingUnit.objects.get(code="18-23-01-003").latitude is None
    kept = PollingUnit.objects.get(code="18-23-01-004")
    assert kept.location_source == "coordinator" and kept.latitude == Decimal("11.070000")
    assert PollingUnit.objects.get(code="18-23-01-005").latitude is None

    # Re-running changes nothing; a moved INEC point is updated.
    assert load_inec_locations(path)["unchanged"] == 2
    write(tmp_path / "inec.csv", [("18-23-01-001", "11.0650", "7.7170", "ok")])
    assert load_inec_locations(path)["updated"] == 1
    assert PollingUnit.objects.get(code="18-23-01-001").latitude == Decimal("11.065000")


def test_coordinator_checked_units_are_not_overwritten(tmp_path):
    PollingUnit.objects.filter(code="18-23-01-001").update(
        latitude="11.064200",
        longitude="7.716036",
        location_source="inec",
        mapping_status="confirmed",
    )
    path = write(tmp_path / "inec.csv", [("18-23-01-001", "11.0700", "7.7200", "ok")])
    assert load_inec_locations(path)["kept_checked_by_coordinator"] == 1
    assert PollingUnit.objects.get(code="18-23-01-001").latitude == Decimal("11.064200")


def test_command(tmp_path, capsys):
    path = write(tmp_path / "inec.csv", [("18-23-01-001", "11.0642", "7.716035636", "ok")])
    call_command("load_inec_locations", file=str(path))
    assert "applied to 1 polling units" in capsys.readouterr().out
