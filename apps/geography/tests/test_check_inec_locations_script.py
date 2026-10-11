"""scripts/check_inec_locations.py helpers that need no map library."""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "check_inec_locations.py"
spec = importlib.util.spec_from_file_location("check_inec_locations", SCRIPT)
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)

HEADER = "code,name,latitude,longitude,status,maps_url,fetched_at\n"


def test_read_collected_ignores_a_half_written_last_line(tmp_path):
    path = tmp_path / "locator.csv"
    path.write_text(
        HEADER
        + "18-23-01-001,A,11.06,7.71,no_location,,t1\n"
        + "18-23-01-001,A,11.06,7.71,ok,,t2\n"  # a later row for the same unit wins
        + "18-23-01-002,B,11.06,7.71,ok,,t3\n"
        + "18-23-01-003,C,11.0",  # the collector is still writing this one
        encoding="utf-8",
    )
    rows = check.read_collected(path)
    assert set(rows) == {"18-23-01-001", "18-23-01-002"}
    assert rows["18-23-01-001"]["status"] == "ok"


def test_complete_lgas():
    units = {
        "18-23-01-001": {"lga": "KD-ZARIA"},
        "18-23-01-002": {"lga": "KD-ZARIA"},
        "18-10-01-001": {"lga": "KD-KADUNA-SOUTH"},
        "18-10-01-002": {"lga": "KD-KADUNA-SOUTH"},
        "18-11-01-001": {"lga": "KD-KAGARKO"},
    }
    collected = {"18-23-01-001": {}, "18-23-01-002": {}, "18-10-01-001": {}}
    assert check.complete_lgas(collected, units) == {"KD-ZARIA"}


def test_coverage_table():
    units = {
        "18-23-01-001": {"lga": "KD-ZARIA"},
        "18-23-01-002": {"lga": "KD-ZARIA"},
        "18-23-01-003": {"lga": "KD-ZARIA"},
        "18-08-01-001": {"lga": "KD-KACHIA"},
    }
    rows = [
        {"code": "18-23-01-001", "qa": "ok"},
        {"code": "18-23-01-002", "qa": "ok_outside_ward"},
        {"code": "18-23-01-003", "qa": "no_location"},
        {"code": "18-08-01-001", "qa": "outside_lga"},
    ]
    table = check.coverage_table(rows, units, {"KD-ZARIA": "Zaria", "KD-KACHIA": "Kachia"})
    lines = table.splitlines()
    assert lines[2] == "| Kachia | 1 | 0 | 0 | 0 | 1 |"
    assert lines[3] == "| Zaria | 3 | 2 | 1 | 1 | 0 |"
    assert lines[4] == "| **Total** | **4** | **2** | **1** | **1** | **1** |"
