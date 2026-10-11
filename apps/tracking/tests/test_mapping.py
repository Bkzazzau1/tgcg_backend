import io
import json

import pytest
from django.utils import timezone
from PIL import Image
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.core.choices import GeographyLevel, Role
from apps.geography.models import Lga, PollingUnit, State, Ward
from apps.geography.polling_units import load_polling_units
from conftest import PASSWORD

pytestmark = pytest.mark.django_db

UNIT = "18-23-01-001"
SPOT = (11.0855, 7.7199)


@pytest.fixture(autouse=True)
def zaria(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    load_polling_units(lga_codes=["KD-ZARIA"])


def login(**fields):
    user = User.objects.create_user(password=PASSWORD, full_name=fields["access_id"], **fields)
    client = APIClient()
    client.force_authenticate(user)
    return client


def ward_coord(access_id="WC-1"):
    return login(
        access_id=access_id,
        role=Role.WARD_COORDINATOR,
        scope_level=GeographyLevel.WARD,
        ward=Ward.objects.get(code="18-23-01"),
    )


@pytest.fixture
def first():
    return ward_coord("WC-1")


@pytest.fixture
def second():
    return ward_coord("WC-2")


@pytest.fixture
def lga():
    return login(
        access_id="LC-1",
        role=Role.LGA_COORDINATOR,
        scope_level=GeographyLevel.LGA,
        lga=Lga.objects.get(code="KD-ZARIA"),
    )


def photo():
    buffer = io.BytesIO()
    Image.new("RGB", (32, 32), (10, 36, 99)).save(buffer, "JPEG")
    buffer.seek(0)
    buffer.name = "signboard.jpg"
    return buffer


def fix(lat=SPOT[0], lng=SPOT[1], accuracy=8.0, mocked=False):
    return {
        "latitude": f"{lat:.6f}",
        "longitude": f"{lng:.6f}",
        "accuracy_m": accuracy,
        "captured_at": timezone.now().isoformat(),
        "is_mocked": mocked,
    }


def pin(client, unit=UNIT, **overrides):
    data = {
        "gps": json.dumps(overrides.pop("gps", fix())),
        "photo": photo(),
        "at_entrance": True,
        "name_matches": True,
        **overrides,
    }
    return client.post(f"/api/v1/mapping/units/{unit}/pin/", data, format="multipart")


def test_first_pin_sets_location_and_second_coordinator_confirms(first, second):
    response = pin(first)
    assert response.status_code == 201, response.content
    assert response.json()["status"] == "pinned"
    unit = PollingUnit.objects.get(code=UNIT)
    assert unit.mapping_status == "pinned"
    assert unit.location_source == "coordinator"
    assert float(unit.latitude) == pytest.approx(SPOT[0])

    same_person = pin(first, gps=fix(lat=SPOT[0] + 0.0001)).json()
    assert same_person["status"] == "confirmed"
    assert PollingUnit.objects.get(code=UNIT).mapping_status == "pinned"  # needs someone else

    other = pin(second, gps=fix(lat=SPOT[0] + 0.0002)).json()  # about 22 m away
    assert other["status"] == "confirmed"
    assert PollingUnit.objects.get(code=UNIT).mapping_status == "confirmed"


@pytest.mark.parametrize(
    "overrides, field, phrase",
    [
        ({"gps": fix(accuracy=45)}, "gps", "must be 30 m or better"),
        ({"gps": fix(mocked=True)}, "gps", "fake-location"),
        ({"at_entrance": False}, "at_entrance", "main entrance"),
        ({"name_matches": False}, "name_matches", "report a problem"),
    ],
)
def test_pin_guide_rules(first, overrides, field, phrase):
    response = pin(first, **overrides)
    assert response.status_code == 400
    assert phrase in str(response.json()[field])
    assert PollingUnit.objects.get(code=UNIT).mapping_status == "unmapped"


def test_photo_is_required(first):
    response = first.post(
        f"/api/v1/mapping/units/{UNIT}/pin/",
        {"gps": json.dumps(fix()), "at_entrance": True, "name_matches": True},
        format="multipart",
    )
    assert response.status_code == 400 and "photo" in response.json()


def test_far_pin_is_a_conflict_resolved_by_someone_else(first, second, lga):
    pin(first)
    conflict = pin(second, gps=fix(lat=SPOT[0] + 0.003)).json()  # about 330 m away
    assert conflict["status"] == "conflict"
    assert conflict["gps"]["distance_to_pu_m"] > 300
    unit = PollingUnit.objects.get(code=UNIT)
    assert unit.mapping_status == "conflict"
    assert float(unit.latitude) == pytest.approx(SPOT[0])  # unchanged until resolved

    url = f"/api/v1/mapping/pins/{conflict['id']}/accept/"
    assert second.post(url).status_code == 403  # not your own pin
    accepted = lga.post(url)
    assert accepted.status_code == 200
    unit.refresh_from_db()
    assert unit.mapping_status == "confirmed"
    assert float(unit.latitude) == pytest.approx(SPOT[0] + 0.003)

    statuses = [p["status"] for p in lga.get(f"/api/v1/mapping/units/{UNIT}/pins/").json()]
    assert sorted(statuses) == ["accepted", "superseded"]


def test_report_problem(first):
    assert first.post(
        f"/api/v1/mapping/units/{UNIT}/problem/", {"problem": "moved", "note": ""}, format="json"
    ).status_code == 400
    response = first.post(
        f"/api/v1/mapping/units/{UNIT}/problem/",
        {"problem": "moved", "note": "Relocated to the new primary school by the market."},
        format="json",
    )
    assert response.status_code == 201
    assert PollingUnit.objects.get(code=UNIT).mapping_status == "problem"


def test_scope_and_roles(first):
    other_ward = PollingUnit.objects.filter(ward__code="18-23-02").first().code
    assert pin(first, unit=other_ward).status_code == 404
    agent = login(
        access_id="AG-1",
        role=Role.POLLING_UNIT_AGENT,
        scope_level=GeographyLevel.POLLING_UNIT,
        polling_unit=PollingUnit.objects.get(code=UNIT),
    )
    assert pin(agent).status_code == 403
    lagos = login(
        access_id="SC-LA",
        role=Role.STATE_COORDINATOR,
        scope_level=GeographyLevel.STATE,
        state=State.objects.get(code="LA"),
    )
    assert pin(lagos).status_code == 404


def test_unit_list_and_progress(first, second, lga):
    pin(first)
    pin(second, gps=fix(lat=SPOT[0] + 0.0001))
    pin(first, unit="18-23-01-002")
    first.post(
        "/api/v1/mapping/units/18-23-01-003/problem/",
        {"problem": "notFound", "note": "No unit at the listed school."},
        format="json",
    )

    listing = first.get("/api/v1/mapping/units/", {"mapping_status": "unmapped"}).json()
    assert listing["count"] == 51 - 3

    ward = first.get("/api/v1/mapping/progress/").json()
    assert ward["total"] == 51
    assert ward["confirmed"] == 1 and ward["pinned"] == 1 and ward["problem"] == 1
    assert ward["mapped"] == 2
    assert ward["mapped_percent"] == pytest.approx(3.9)

    zaria = lga.get("/api/v1/mapping/progress/").json()
    assert zaria["total"] == 584
    assert len(zaria["children"]) == 13
    ward_row = next(c for c in zaria["children"] if c["code"] == "18-23-01")
    assert ward_row["mapped"] == 2 and ward_row["total"] == 51


def test_pin_photo_is_private(first, lga):
    created = pin(first).json()
    assert lga.get(f"/api/v1/mapping/pins/{created['id']}/photo/").status_code == 200
    lagos = login(
        access_id="SC-LA2",
        role=Role.STATE_COORDINATOR,
        scope_level=GeographyLevel.STATE,
        state=State.objects.get(code="LA"),
    )
    assert lagos.get(f"/api/v1/mapping/pins/{created['id']}/photo/").status_code == 404
