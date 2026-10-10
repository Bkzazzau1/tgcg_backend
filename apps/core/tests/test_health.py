import pytest

pytestmark = pytest.mark.django_db


def test_health_is_public(api_client):
    response = api_client.get("/api/v1/health/")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


def test_openapi_schema_builds(api_client):
    assert api_client.get("/api/schema/").status_code == 200
