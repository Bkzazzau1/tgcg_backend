import pytest
from django.core.management import call_command

from apps.geography.models import Lga, SenatorialDistrict, State, Zone

pytestmark = pytest.mark.django_db


def test_migration_seeds_canonical_geography():
    assert Zone.objects.count() == 6
    assert State.objects.count() == 37
    assert SenatorialDistrict.objects.count() == 109
    assert Lga.objects.count() == 774
    assert State.objects.get(code="FCT").is_fct


def test_lga_codes_match_flutter_registry():
    lga = Lga.objects.select_related("state__zone", "senatorial_district").get(
        code="KD-KADUNA-NORTH"
    )
    assert lga.name == "Kaduna North"
    assert lga.state.code == "KD"
    assert lga.state.zone.code == "NW"
    assert lga.senatorial_district.state_id == lga.state_id


def test_every_state_lga_count_matches_districts():
    for state in State.objects.all():
        lgas = state.lgas.count()
        in_districts = Lga.objects.filter(senatorial_district__state=state).count()
        assert lgas == in_districts, state.code


def test_load_geography_command_is_idempotent():
    call_command("load_geography")
    assert Lga.objects.count() == 774


def test_geography_requires_authentication(api_client):
    assert api_client.get("/api/v1/geography/states/").status_code == 401


def test_list_states(auth_client):
    response = auth_client.get("/api/v1/geography/states/")
    assert response.status_code == 200
    assert len(response.json()) == 37


def test_filter_lgas_by_state(auth_client):
    response = auth_client.get("/api/v1/geography/lgas/", {"state__code": "LA"})
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 20
    assert {"code": "LA-IKEJA", "slug": "ikeja", "name": "Ikeja"}.items() <= next(
        item for item in body["results"] if item["code"] == "LA-IKEJA"
    ).items()


def test_senatorial_district_lookup_by_slashed_code(auth_client):
    district = SenatorialDistrict.objects.first()
    response = auth_client.get(f"/api/v1/geography/senatorial-districts/{district.code}/")
    assert response.status_code == 200
    assert response.json()["code"] == district.code
