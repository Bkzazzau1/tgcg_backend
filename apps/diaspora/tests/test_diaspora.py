"""Joining from the diaspora (3 November 2026): a number abroad, where the
member lives, their home in Nigeria with or without a PVC, and the counts by
country on Members & Exco."""

import pytest
from rest_framework.test import APIClient

from apps.access.tests.test_access import client_for
from apps.accounts.models import User
from apps.accounts.phone import normalize_phone
from apps.core.choices import GeographyLevel, Role
from apps.diaspora.models import DiasporaProfile
from apps.geography.models import Ward
from apps.geography.polling_units import load_polling_units
from apps.membership.models import Member
from apps.membership.serializers import MemberSerializer
from conftest import PASSWORD

# Joining runs outside the request transaction, as in the registration tests.
pytestmark = pytest.mark.django_db(transaction=True, serialized_rollback=True)

VIN = "90F5B0E8D0295847263"
PIN = "482915"
LONDON = "+44 7700 900123"


@pytest.fixture(autouse=True)
def setup(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    settings.REGISTRATION_PHONE_CODE_REQUIRED = False
    settings.SUMMARY_CACHE_SECONDS = 0
    load_polling_units(lga_codes=["KD-ZARIA"])


def join(**overrides):
    data = {
        "phone_number": LONDON,
        "full_name": "Amina Yusuf",
        "email": "amina@example.com",
        "residence_country": "GB",
        "residence_city": "London",
        "residence_address": "12 High Street, Croydon",
        "home_lga": "KD-ZARIA",
        "pin": PIN,
        "consent": True,
        **overrides,
    }
    return APIClient().post("/api/v1/register/", data, format="multipart")


def sign_in(username):
    return APIClient().post(
        "/api/v1/auth/login/", {"username": username, "password": PIN}, format="json"
    )


@pytest.mark.parametrize(
    ("written", "stored"),
    [
        ("+44 7700 900123", "+447700900123"),
        ("0044 7700 900123", "+447700900123"),
        ("+44 (0) 7700 900123", "+447700900123"),
        ("+44 07700 900123", "+447700900123"),  # the 0 dialled at home
        ("+234 0803 123 4567", "+2348031234567"),
        ("+225 07 12 34 56 78", "+2250712345678"),  # here the 0 is the number's
        ("+1 (202) 555-0143", "+12025550143"),
        # Read by its country code, never as a Nigerian mobile.
        ("+82 2 123 4567", "+8221234567"),
        ("+234 803 123 4567", "+2348031234567"),
        ("0803 123 4567", "+2348031234567"),
        ("447700900123", None),  # abroad needs + or 00
        ("+234 1 234 5678", None),  # +234 is a Nigerian mobile or nothing
        ("+0 123 4567", None),
        ("+44 12", None),
    ],
)
def test_numbers_abroad_are_read_by_their_country_code(written, stored):
    assert normalize_phone(written) == stored


def test_joining_from_abroad_without_a_pvc():
    response = join()
    assert response.status_code == 201, response.content
    member = Member.objects.get()
    assert member.phone_number == "+447700900123" and member.vin is None
    assert member.scope_level == GeographyLevel.LGA and member.lga.code == "KD-ZARIA"
    assert member.state.code == "KD" and member.email == "amina@example.com"
    # No SMS reaches a number abroad, so it is not recorded as confirmed.
    assert member.phone_verified_at is None and member.user.role == Role.MEMBER
    profile = DiasporaProfile.objects.get(member=member)
    assert (profile.country, profile.city) == ("GB", "London")
    assert profile.address == "12 High Street, Croydon"

    for username in (LONDON, "00447700900123", "+44 07700 900123"):
        login = sign_in(username)
        assert login.status_code == 200, (username, login.content)


def test_with_a_ward_the_home_is_the_ward():
    ward = Ward.objects.filter(lga__code="KD-ZARIA").order_by("code").first()
    assert join(home_ward=ward.code).status_code == 201
    member = Member.objects.get()
    assert member.scope_level == GeographyLevel.WARD and member.ward == ward


def test_with_a_pvc_the_home_is_the_polling_unit():
    response = join(vin=VIN, delim="18/23/01/001", home_lga="")
    assert response.status_code == 201, response.content
    member = Member.objects.get()
    assert member.vin == VIN and member.polling_unit.code == "18-23-01-001"
    assert member.diaspora.country == "GB"


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"email": ""}, "email"),
        ({"residence_country": "NG"}, "residence_country"),
        ({"residence_country": "XX"}, "residence_country"),
        ({"residence_city": " "}, "residence_city"),
        ({"home_lga": ""}, "home_lga"),
        ({"home_lga": "KD-NOWHERE"}, "home_lga"),
        ({"home_ward": "99-99-99"}, "home_ward"),
        # Written as at home, a number could pass for a Nigerian one.
        ({"phone_number": "07700 900123"}, "phone_number"),
        ({"phone_number": "0803 123 4567"}, "phone_number"),
        ({"vin": VIN}, "delim"),  # a PVC gives its polling unit too
        ({"code": "123456"}, "code"),  # codes go to Nigerian numbers only
    ],
)
def test_the_diaspora_form_says_what_is_missing(overrides, field):
    response = join(**overrides)
    assert response.status_code == 400 and field in response.json(), response.content
    assert not Member.objects.exists() and not DiasporaProfile.objects.exists()


def test_the_usual_form_still_takes_nigerian_numbers_and_a_pvc():
    usual = {"residence_country": "", "residence_city": ""}
    abroad = join(**usual, vin=VIN, delim="18/23/01/001")
    assert abroad.status_code == 400 and "phone_number" in abroad.json()
    no_pvc = join(**usual, phone_number="08031234567")
    assert no_pvc.status_code == 400 and "vin" in no_pvc.json()
    assert not Member.objects.exists()


def test_a_number_abroad_joins_even_while_nigerian_numbers_need_a_code(settings):
    settings.REGISTRATION_PHONE_CODE_REQUIRED = True
    nigerian = join(phone_number="+234 803 123 4567")
    assert nigerian.status_code == 400 and "code" in nigerian.json()
    assert join().status_code == 201
    assert Member.objects.get().phone_verified_at is None


def test_a_number_abroad_joins_once_however_it_is_written():
    assert join().status_code == 201
    again = join(phone_number="0044 7700 900123", email="other@example.com")
    assert again.status_code == 400 and "phone_number" in again.json()
    assert Member.objects.count() == 1


def test_the_countries_to_live_in_leave_out_nigeria():
    countries = APIClient().get("/api/v1/register/places/countries/").json()
    assert {"code": "GB", "name": "United Kingdom", "calling_code": "44"} in countries
    assert {"code": "US", "name": "United States", "calling_code": "1"} in countries
    assert "NG" not in {country["code"] for country in countries}
    names = [country["name"] for country in countries]
    assert names == sorted(names)


def test_the_national_administrator_counts_the_diaspora_by_country():
    assert join().status_code == 201
    assert join(phone_number="+44 7700 900124", email="b@example.com").status_code == 201
    assert (
        join(
            phone_number="+1 202 555 0143",
            email="c@example.com",
            residence_country="US",
            residence_city="Houston",
        ).status_code
        == 201
    )
    at_home = join(
        residence_country="",
        residence_city="",
        phone_number="08031234567",
        vin=VIN,
        delim="18/23/01/001",
    )
    assert at_home.status_code == 201
    admin = User.objects.create_user(
        "NA-1", PASSWORD, full_name="National Admin", role=Role.NATIONAL_ADMINISTRATOR
    )
    client = client_for(admin)

    figures = client.get("/api/v1/members/analysis/").json()["members"]
    assert figures["total"] == 4
    assert figures["diaspora"] == {
        "total": 3,
        "countries": [
            {"country": "GB", "name": "United Kingdom", "count": 2},
            {"country": "US", "name": "United States", "count": 1},
        ],
    }
    # Members abroad count under their home too.
    kaduna = client.get("/api/v1/members/analysis/", {"level": "state", "code": "KD"}).json()
    assert kaduna["members"]["diaspora"]["total"] == 3

    in_britain = client.get("/api/v1/members/", {"country": "gb"}).json()
    assert in_britain["count"] == 2
    row = in_britain["results"][0]
    assert row["diaspora"]["country_name"] == "United Kingdom"
    assert row["diaspora"]["address"] == "12 High Street, Croydon"
    assert client.get("/api/v1/members/", {"show": "diaspora"}).json()["count"] == 3
    found = client.get("/api/v1/members/", {"q": "+1 202 555 0143"}).json()
    assert [row["diaspora"]["city"] for row in found["results"]] == ["Houston"]


def test_the_address_abroad_is_personal():
    assert join().status_code == 201
    shown = MemberSerializer(Member.objects.get(), context={}).data
    assert shown["diaspora"] == {
        "country": "GB",
        "country_name": "United Kingdom",
        "city": "London",
        "address": None,
    }
    assert shown["phone_number"] is None
