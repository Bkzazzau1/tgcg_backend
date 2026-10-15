import hashlib

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from apps.field.models import IncidentEvent

pytestmark = pytest.mark.django_db

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"tgcg-evidence"
INCIDENTS = "/api/v1/incidents/"
REPORTS = "/api/v1/field-reports/"


def report(client, **extra):
    payload = {
        "title": "Ballot box  snatched",
        "category": "Violence / threat",
        "severity": "critical",
        "summary": "Armed group at the unit.",
        **extra,
    }
    if "latitude" not in extra and "gps" not in extra:
        # The app sends a GPS fix; from a polling unit it must.
        payload["gps"] = {
            "latitude": "10.523000",
            "longitude": "7.438000",
            "accuracy_m": 12,
            "captured_at": timezone.now().isoformat(),
            "is_mocked": False,
        }
    return client.post(INCIDENTS, payload, format="json")


def upload(client, reference, content=JPEG, name="scene.jpg", base=INCIDENTS, **extra):
    data = {"file": SimpleUploadedFile(name, content), "type": "photo", **extra}
    return client.post(f"{base}{reference}/evidence/", data, format="multipart")


# --- reporting ------------------------------------------------------------


def test_agent_reports_incident_at_own_polling_unit(agent):
    response = report(agent, latitude="10.523", longitude="7.438")
    assert response.status_code == 201, response.content
    body = response.json()
    assert body["reference"] == "INC-000001"
    assert body["title"] == "Ballot box snatched"
    assert body["status"] == "reported"
    assert body["is_open"] is True
    assert body["reporter"] == "AG-1"
    assert body["scope"]["pollingUnitId"] == "KD-KN-W01-PU001"
    assert body["scope"]["lgaId"] == "KD-KADUNA-NORTH"
    assert IncidentEvent.objects.filter(action="reported").count() == 1


def test_reports_from_a_polling_unit_need_gps(agent, coordinator):
    payload = {"title": "No lights", "category": "Materials / logistics", "severity": "low"}
    refused = agent.post(INCIDENTS, payload, format="json")
    assert refused.status_code == 400
    assert "gps" in refused.json()
    # A coordinator may still report from an office computer.
    assert coordinator.post(INCIDENTS, payload, format="json").status_code == 201


def test_agent_cannot_report_outside_scope(agent):
    response = report(agent, scope={"level": "lga", "lgaId": "LA-IKEJA"})
    assert response.status_code == 403


def test_location_must_be_in_nigeria(agent):
    assert report(agent, latitude="51.5", longitude="-0.12").status_code == 400
    assert report(agent, latitude="10.5").status_code == 400


def test_unknown_category_rejected(agent):
    assert report(agent, category="Gossip").status_code == 400


def test_client_id_makes_report_idempotent(agent):
    first = report(agent, client_id="dev-1:INC-0001").json()
    retry = report(agent, client_id="dev-1:INC-0001")
    assert retry.status_code == 201
    assert retry.json()["reference"] == first["reference"]


def test_visibility(agent, other_agent, coordinator, lagos):
    reference = report(agent).json()["reference"]
    assert agent.get(INCIDENTS).json()["count"] == 1
    assert other_agent.get(INCIDENTS).json()["count"] == 0
    assert other_agent.get(f"{INCIDENTS}{reference}/").status_code == 404
    assert coordinator.get(INCIDENTS).json()["count"] == 1
    assert lagos.get(INCIDENTS).json()["count"] == 0


def test_filters(agent, coordinator):
    report(agent, severity="low", category="Technical")
    report(agent, severity="high")
    assert coordinator.get(INCIDENTS, {"min_severity": "high"}).json()["count"] == 1
    assert coordinator.get(INCIDENTS, {"open": "true"}).json()["count"] == 2
    assert coordinator.get(INCIDENTS, {"category": "Technical"}).json()["count"] == 1
    listed = coordinator.get(INCIDENTS).json()["results"]
    assert [item["severity"] for item in listed] == ["high", "low"]  # most severe first


# --- workflow -------------------------------------------------------------


def test_workflow_and_audit_trail(agent, coordinator, director):
    ref = report(agent).json()["reference"]
    status_url = f"{INCIDENTS}{ref}/status/"

    assert agent.post(status_url, {"status": "acknowledged"}).status_code == 403
    acknowledged = coordinator.post(status_url, {"status": "acknowledged"})
    assert acknowledged.status_code == 200
    assert acknowledged.json()["acknowledged_at"]

    assert coordinator.post(f"{INCIDENTS}{ref}/assign/", {"team": " "}).status_code == 400
    assigned = coordinator.post(f"{INCIDENTS}{ref}/assign/", {"team": "Kaduna Police Desk"})
    assert assigned.json()["status"] == "assigned"
    assert assigned.json()["assigned_team"] == "Kaduna Police Desk"

    assert coordinator.post(status_url, {"status": "escalated"}).status_code == 400  # note
    assert (
        coordinator.post(status_url, {"status": "escalated", "note": "Shots fired"}).status_code
        == 200
    )
    resolved = coordinator.post(status_url, {"status": "resolved", "note": "Area secured"})
    assert resolved.status_code == 200
    assert resolved.json()["is_open"] is False

    # Closing needs closeIncident, which LGA coordinators do not hold.
    assert coordinator.post(status_url, {"status": "closed", "note": "Done"}).status_code == 403
    closed = director.post(status_url, {"status": "closed", "note": "Verified closed"})
    assert closed.status_code == 200
    assert closed.json()["closed_at"]
    assert director.post(status_url, {"status": "investigating", "note": "x"}).status_code == 400

    events = coordinator.get(f"{INCIDENTS}{ref}/events/").json()
    assert [e["to_status"] for e in events] == [
        "reported",
        "acknowledged",
        "assigned",
        "escalated",
        "resolved",
        "closed",
    ]
    assert events[2]["note"] == "Assigned to Kaduna Police Desk."
    assert events[5]["actor"] == "SRD-1"


def test_reopen_requires_note(agent, coordinator):
    ref = report(agent).json()["reference"]
    url = f"{INCIDENTS}{ref}/status/"
    coordinator.post(url, {"status": "acknowledged"})
    coordinator.post(url, {"status": "resolved", "note": "Calm"})
    assert coordinator.post(url, {"status": "investigating"}).status_code == 400
    reopened = coordinator.post(url, {"status": "investigating", "note": "Renewed clash"})
    assert reopened.status_code == 200
    assert reopened.json()["resolved_at"] is None


def test_invalid_transition(agent, coordinator):
    ref = report(agent).json()["reference"]
    response = coordinator.post(f"{INCIDENTS}{ref}/status/", {"status": "resolved", "note": "x"})
    assert response.status_code == 400


# --- evidence -------------------------------------------------------------


def test_reporter_uploads_and_coordinator_downloads(agent, coordinator, lagos):
    ref = report(agent).json()["reference"]
    sha = hashlib.sha256(JPEG).hexdigest()
    response = upload(agent, ref, content_hash=sha, caption="Scene", client_id="dev-1:EV-1")
    assert response.status_code == 201, response.content
    evidence = response.json()
    assert evidence["content_hash"] == sha
    assert evidence["mime_type"] == "image/jpeg"
    assert evidence["size"] == len(JPEG)
    assert evidence["uploader"] == "AG-1"

    retry = upload(agent, ref, client_id="dev-1:EV-1")
    assert retry.json()["id"] == evidence["id"]

    incident = coordinator.get(f"{INCIDENTS}{ref}/").json()
    assert len(incident["evidence"]) == 1
    download = coordinator.get(incident["evidence"][0]["download_url"])
    assert download.status_code == 200
    assert b"".join(download.streaming_content) == JPEG
    assert download["X-Content-SHA256"] == sha
    assert "attachment" in download["Content-Disposition"]

    assert lagos.get(evidence["download_url"]).status_code == 404
    assert IncidentEvent.objects.filter(action="evidenceAdded").count() == 1


def test_evidence_integrity_checks(agent, settings):
    ref = report(agent).json()["reference"]
    assert upload(agent, ref, content=b"not really a jpeg").status_code == 400
    assert upload(agent, ref, name="payload.exe").status_code == 400
    assert upload(agent, ref, content_hash="0" * 64).status_code == 400
    assert upload(agent, ref, content=b"").status_code == 400
    settings.EVIDENCE_MAX_UPLOAD_MB = 0
    assert upload(agent, ref).status_code == 400


def test_only_reporter_or_evidence_managers_add_evidence(agent, coordinator, other_agent):
    ref = report(agent).json()["reference"]
    assert upload(other_agent, ref).status_code == 404  # cannot even see it
    assert upload(coordinator, ref).status_code == 403  # sees it, lacks manageEvidence


def test_no_evidence_on_closed_incident(agent, coordinator, director):
    ref = report(agent).json()["reference"]
    director.post(f"{INCIDENTS}{ref}/status/", {"status": "closed", "note": "Duplicate"})
    assert upload(agent, ref).status_code == 400


# --- field reports --------------------------------------------------------


def test_field_report_review(agent, coordinator):
    ref = report(agent).json()["reference"]
    response = agent.post(
        REPORTS,
        {"category": "Opening status", "summary": "Opened 08:10", "incident": ref},
        format="json",
    )
    assert response.status_code == 201, response.content
    rpt = response.json()
    assert rpt["reference"] == "RPT-000001"
    assert rpt["incident"] == ref
    assert rpt["status"] == "submitted"

    url = f"{REPORTS}{rpt['reference']}/review/"
    assert agent.post(url, {"accepted": True}).status_code == 403
    assert coordinator.post(url, {"accepted": False}).status_code == 400  # reason
    accepted = coordinator.post(url, {"accepted": True, "reason": "Matches call log"})
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "verified"
    assert accepted.json()["reviewed_by"] == "LC-KN"
    assert coordinator.post(url, {"accepted": False, "reason": "x"}).status_code == 400

    assert upload(agent, rpt["reference"], base=REPORTS).status_code == 201


def test_cannot_review_own_report(coordinator):
    rpt = coordinator.post(
        REPORTS, {"category": "Operational update", "summary": "All calm"}, format="json"
    ).json()
    response = coordinator.post(f"{REPORTS}{rpt['reference']}/review/", {"accepted": True})
    assert response.status_code == 403


def test_cannot_link_report_to_invisible_incident(agent, other_agent):
    ref = report(agent).json()["reference"]
    response = other_agent.post(
        REPORTS, {"category": "Other", "summary": "Saw it", "incident": ref}, format="json"
    )
    assert response.status_code == 400
    assert "incident" in response.json()
