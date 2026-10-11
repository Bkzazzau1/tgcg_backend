"""scripts/inec_locator.py, tested without touching INEC's servers."""

import csv
import importlib.util
import io
import json
import urllib.error
from email.message import Message
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "inec_locator.py"
spec = importlib.util.spec_from_file_location("inec_locator", SCRIPT)
locator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(locator)

REDIRECT = "https://maps.google.com/?q=11.0539,7.712954605&mra=pd&t=m&z=3"


def headers(**values):
    message = Message()
    for key, value in values.items():
        message[key] = value
    return message


class Response:
    def __init__(self, body="", status=200):
        self.status, self.headers, self._body = status, headers(), body.encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code, **values):
    return urllib.error.HTTPError("https://x", code, "", headers(**values), io.BytesIO(b""))


class Opener:
    """Plays back responses or errors in order."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def make(*outcomes, delay=1.0):
    sleeps = []
    return locator.Locator(delay, opener=Opener(*outcomes), sleep=sleeps.append), sleeps


def test_extract_location():
    assert locator.extract_location(REDIRECT, "") == ("11.0539", "7.712954605", REDIRECT)
    assert locator.extract_location("https://maps.google.com/?q=0,0&mra=pd", "") is None
    page = '<a href="https://www.google.com/maps/dir/?api=1&amp;destination=10.5%2C7.4">Go</a>'
    assert locator.extract_location(None, page)[:2] == ("10.5", "7.4")
    assert locator.extract_location(None, "<p>No location</p>") is None


def test_parse_lists():
    page = (
        '<select name="data[Search][state_id]" id="SearchStateId">'
        '<option value="">--</option><option value="19">18 - KADUNA</option></select>'
    )
    assert locator.parse_states(page) == [(19, 18, "KADUNA")]
    body = json.dumps([{"0": "--SELECT--", "selected": "0", "4244": '01 - KWARBAI "A"'}])
    assert locator.parse_choices(body) == [(4244, 1, 'KWARBAI "A"')]


def test_redirect_is_returned_not_followed():
    client, _ = make(http_error(302, Location=REDIRECT))
    status, response_headers, _ = client.directions(19, 369, 4244, 56450)
    assert status == 302 and response_headers.get("Location") == REDIRECT
    form = client.opener.requests[0].data.decode()
    assert "polling_unit_id%5D=56450" in form


def test_server_errors_wait_then_slow_down(capsys):
    client, sleeps = make(http_error(503), http_error(502), http_error(302, Location=REDIRECT))
    status, _, _ = client.directions(19, 369, 4244, 56450)
    assert status == 302
    assert [s for s in sleeps if s >= 30] == [30, 60]
    assert client.delay == 4.0  # halved speed twice, capped at one request every 4 s
    log = capsys.readouterr().out
    assert "HTTP 503" in log and "HTTP 502" in log


def test_lost_connection_is_waited_out_without_slowing_down(capsys):
    offline = urllib.error.URLError("[Errno 11001] getaddrinfo failed")
    client, sleeps = make(*[offline] * 20, http_error(302, Location=REDIRECT))
    status, _, _ = client.directions(19, 369, 4244, 56450)
    assert status == 302  # 20 minutes offline did not stop the run
    assert [s for s in sleeps if s >= 30] == [locator.OFFLINE_WAIT] * 20
    assert client.delay == 1.0
    assert "no connection" in capsys.readouterr().out


def test_speeds_back_up_after_good_answers():
    client, _ = make(http_error(503), *[Response()] * locator.RECOVER_AFTER)
    client.request("/pu")
    assert client.delay == 2.0
    for _ in range(locator.RECOVER_AFTER - 1):
        client.request("/pu")
    assert client.delay == 1.0


def test_gives_up_after_about_fifteen_minutes():
    client, sleeps = make(*[http_error(503)] * 6)
    with pytest.raises(SystemExit, match="kept failing"):
        client.directions(19, 369, 4244, 56450)
    assert [s for s in sleeps if s >= 30] == list(locator.RETRY_WAITS)


def test_refusal_stops_at_once():
    client, sleeps = make(http_error(403))
    with pytest.raises(SystemExit, match="HTTP 403"):
        client.directions(19, 369, 4244, 56450)
    assert sleeps == []


class FakeLocator:
    """Answers like the locator for one state, one LGA, one ward and two units."""

    def __init__(self):
        self.directions_asked = []

    def request(self, path, **kwargs):
        page = '<select id="SearchStateId"><option value="19">18 - KADUNA</option></select>'
        return 200, headers(), page

    def choices(self, kind, parent_field, parent_id):
        return {
            "lgas": [(369, 23, "ZARIA")],
            "wards": [(4244, 1, 'KWARBAI "A"')],
            "pus": [(56437, 1, "KWARBAI I"), (56450, 14, "UNG. ASKA I")],
        }[kind]

    def directions(self, state_id, lga_id, ward_id, unit_id):
        self.directions_asked.append(unit_id)
        if unit_id == 56437:
            return 302, headers(Location="https://maps.google.com/?q=0,0"), ""
        return 302, headers(Location=REDIRECT), ""


def test_harvest_writes_rows_and_resumes(tmp_path, capsys):
    out = tmp_path / "locator.csv"
    fake = FakeLocator()
    assert locator.harvest(fake, out, state=18) == (2, 1)
    rows = {row["code"]: row for row in csv.DictReader(out.open(encoding="utf-8"))}
    assert rows["18-23-01-014"]["latitude"] == "11.0539"
    assert rows["18-23-01-014"]["status"] == "ok"
    missing = rows["18-23-01-001"]
    assert missing["status"] == "no_location"
    assert missing["maps_url"] == "https://maps.google.com/?q=0,0"  # kept to explain it

    # A re-run fetches nothing again, unless asked to retry missing units.
    again = FakeLocator()
    assert locator.harvest(again, out, state=18) == (0, 0)
    assert again.directions_asked == []
    retry = FakeLocator()
    locator.harvest(retry, out, state=18, retry_missing=True)
    assert retry.directions_asked == [56437]


def test_damaged_list_cache_is_rebuilt(tmp_path):
    out = tmp_path / "locator.csv"
    Path(str(out) + ".lists.json").write_text('{"lgas/19": [[369, 23', encoding="utf-8")
    assert locator.harvest(FakeLocator(), out, state=18) == (2, 1)
    cache = json.loads(Path(str(out) + ".lists.json").read_text(encoding="utf-8"))
    assert cache["units/4244"] == [[56437, 1, "KWARBAI I"], [56450, 14, "UNG. ASKA I"]]


class ManyUnits(FakeLocator):
    """One ward of 15 units; INEC has coordinates only for units 13-15."""

    def choices(self, kind, parent_field, parent_id):
        if kind == "pus":
            return [(1000 + n, n, f"UNIT {n}") for n in range(1, 16)]
        return super().choices(kind, parent_field, parent_id)

    def directions(self, state_id, lga_id, ward_id, unit_id):
        self.directions_asked.append(unit_id)
        if unit_id - 1000 >= 13:
            return 302, headers(Location=REDIRECT), ""
        return 302, headers(Location="https://maps.google.com/?q=UNIT, ZARIA, KADUNA"), ""


def test_guard_stops_when_new_units_have_no_coordinates(tmp_path):
    with pytest.raises(SystemExit, match="first 10 new units"):
        locator.harvest(ManyUnits(), tmp_path / "locator.csv", state=18)


def test_guard_ignores_units_retried_for_missing_locations(tmp_path):
    out = tmp_path / "locator.csv"
    with out.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, locator.FIELDS)
        writer.writeheader()
        for n in range(1, 13):  # 12 units INEC could not place last time
            writer.writerow({"code": f"18-23-01-{n:03d}", "status": "no_location"})
    # Retrying them first must not look like a format change.
    retry = ManyUnits()
    assert locator.harvest(retry, out, state=18, retry_missing=True) == (15, 3)
