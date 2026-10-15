"""Incident, field report and evidence rules. All writes go through here."""

import hashlib
import mimetypes
from pathlib import Path

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.access import engine
from apps.core.choices import EvidenceType, GeographyLevel, IncidentStatus, RecordStatus
from apps.core.models import Sequence
from apps.core.permissions import Capability, has_capability
from apps.core.uploads import PHOTO_EXTENSIONS, VIDEO_EXTENSIONS, matches_signature
from apps.geography.scope import default_to_actor_scope, scope_contains

from .models import Evidence, FieldReport, Incident, IncidentEvent

C = Capability
I = IncidentStatus  # noqa: E741

# --- incident workflow -------------------------------------------------------

# Status moves made through change_status(). "assigned" is reached via assign().
TRANSITIONS = {
    I.REPORTED: {I.ACKNOWLEDGED, I.ESCALATED, I.CLOSED},
    I.ACKNOWLEDGED: {I.INVESTIGATING, I.ESCALATED, I.RESOLVED, I.CLOSED},
    I.ASSIGNED: {I.INVESTIGATING, I.ESCALATED, I.RESOLVED},
    I.INVESTIGATING: {I.ESCALATED, I.RESOLVED},
    I.ESCALATED: {I.INVESTIGATING, I.RESOLVED},
    I.RESOLVED: {I.INVESTIGATING, I.CLOSED},  # investigating = reopen
    I.CLOSED: set(),
}
ASSIGNABLE_FROM = {I.REPORTED, I.ACKNOWLEDGED, I.ASSIGNED, I.INVESTIGATING, I.ESCALATED}

# Capability needed to move an incident into each status.
STATUS_CAPABILITY = {
    I.ACKNOWLEDGED: (C.ACKNOWLEDGE_INCIDENT,),
    I.INVESTIGATING: (C.ACKNOWLEDGE_INCIDENT,),
    I.ESCALATED: (C.ACKNOWLEDGE_INCIDENT,),
    I.RESOLVED: (C.ASSIGN_INCIDENT, C.CLOSE_INCIDENT),
    I.CLOSED: (C.CLOSE_INCIDENT,),
}
NOTE_REQUIRED = {I.ESCALATED, I.RESOLVED, I.CLOSED}


def _require_scope(actor, target, what, permission):
    if not scope_contains(actor, target, permission=permission):
        raise PermissionDenied(f"The {what} is outside your operational scope.")


def _require_any(actor, target, permissions, refusal):
    """One of `permissions` must be held by a grant that covers `target`."""
    if any(scope_contains(actor, target, permission=p) for p in permissions):
        return
    if has_capability(actor, *permissions):
        raise PermissionDenied("The incident is outside your operational scope.")
    raise PermissionDenied(refusal)


def _save(instance, exclude=None):
    instance.full_clean(exclude=exclude)
    try:
        with transaction.atomic():
            instance.save()
    except IntegrityError as exc:
        raise ValidationError("This record conflicts with an existing one.") from exc


def reports_only_at_polling_units(actor) -> bool:
    """Agents and polling unit executives: every area where they report
    incidents is a polling unit."""
    areas = engine.areas_for(actor, C.CREATE_INCIDENT)
    return bool(areas) and all(area.scope_level == GeographyLevel.POLLING_UNIT for area in areas)


@transaction.atomic
def report_incident(*, actor, **fields) -> Incident:
    client_id = fields.get("client_id")
    if client_id:
        existing = Incident.objects.filter(client_id=client_id, reporter=actor).first()
        if existing is not None:
            return existing  # offline retry
    default_to_actor_scope(actor, fields, C.CREATE_INCIDENT)
    gps = fields.pop("gps", None)
    if gps is None and fields.get("latitude") is not None:
        # Older clients send bare coordinates without accuracy.
        gps = {"latitude": fields["latitude"], "longitude": fields.get("longitude")}
    # Reports from a polling unit say where they were made: how far from the
    # unit. Coordinators may still report from an office computer.
    if gps is None and reports_only_at_polling_units(actor):
        raise ValidationError(
            {"gps": "Turn on location: a report from your polling unit needs GPS."}
        )
    incident = Incident(reporter=actor, status=I.REPORTED, **fields)
    incident.normalize_scope()
    incident.apply_fix(gps, incident.polling_unit)
    incident.full_clean(exclude=["reference", "severity_rank"])
    _require_scope(actor, incident, "incident location", C.CREATE_INCIDENT)
    incident.reference = f"INC-{Sequence.next_value('incident'):06d}"
    _save(incident, exclude=["severity_rank"])
    IncidentEvent.objects.create(
        incident=incident,
        action=IncidentEvent.Action.REPORTED,
        to_status=I.REPORTED,
        actor=actor,
    )
    return incident


def _lock(incident):
    return Incident.objects.select_for_update().get(pk=incident.pk)


@transaction.atomic
def change_incident_status(*, actor, incident, status, note="") -> Incident:
    incident = _lock(incident)
    note = note.strip()
    _require_any(
        actor,
        incident,
        STATUS_CAPABILITY.get(status, ()),
        "Your role cannot move an incident to this status.",
    )
    if status == incident.status:
        raise ValidationError({"status": f"The incident is already {status}."})
    if status not in TRANSITIONS[incident.status]:
        raise ValidationError({"status": f"Cannot change from {incident.status} to {status}."})
    reopening = incident.status == I.RESOLVED and status == I.INVESTIGATING
    if (status in NOTE_REQUIRED or reopening) and not note:
        raise ValidationError({"note": "A note is required for this change."})

    previous = incident.status
    incident.status = status
    now = timezone.now()
    if status == I.ACKNOWLEDGED:
        incident.acknowledged_at = now
    elif status == I.RESOLVED:
        incident.resolved_at = now
    elif status == I.CLOSED:
        incident.closed_at = now
    elif reopening:
        incident.resolved_at = None
    _save(incident, exclude=["severity_rank"])
    IncidentEvent.objects.create(
        incident=incident,
        action=IncidentEvent.Action.STATUS_CHANGED,
        from_status=previous,
        to_status=status,
        note=note,
        actor=actor,
    )
    return incident


@transaction.atomic
def assign_incident(*, actor, incident, team, note="") -> Incident:
    incident = _lock(incident)
    _require_any(actor, incident, (C.ASSIGN_INCIDENT,), "Your role cannot assign incidents.")
    team = " ".join(team.split())
    if not team:
        raise ValidationError({"team": "A response team is required."})
    if incident.status not in ASSIGNABLE_FROM:
        raise ValidationError({"status": f"A {incident.status} incident cannot be assigned."})

    previous = incident.status
    incident.status = I.ASSIGNED
    incident.assigned_team = team
    incident.acknowledged_at = incident.acknowledged_at or timezone.now()
    _save(incident, exclude=["severity_rank"])
    IncidentEvent.objects.create(
        incident=incident,
        action=IncidentEvent.Action.ASSIGNED,
        from_status=previous,
        to_status=I.ASSIGNED,
        note=f"Assigned to {team}." + (f" {note.strip()}" if note.strip() else ""),
        actor=actor,
    )
    return incident


# --- field reports -----------------------------------------------------------


@transaction.atomic
def submit_report(*, actor, **fields) -> FieldReport:
    client_id = fields.get("client_id")
    if client_id:
        existing = FieldReport.objects.filter(client_id=client_id, reporter=actor).first()
        if existing is not None:
            return existing
    default_to_actor_scope(actor, fields, C.SUBMIT_FIELD_REPORT)
    report = FieldReport(reporter=actor, status=RecordStatus.SUBMITTED, **fields)
    report.full_clean(exclude=["reference"])
    _require_scope(actor, report, "report location", C.SUBMIT_FIELD_REPORT)
    if report.incident is not None:
        _require_scope(actor, report.incident, "linked incident", C.SUBMIT_FIELD_REPORT)
    report.reference = f"RPT-{Sequence.next_value('field_report'):06d}"
    _save(report)
    return report


@transaction.atomic
def review_report(*, actor, report, accepted: bool, reason="") -> FieldReport:
    report = FieldReport.objects.select_for_update().get(pk=report.pk)
    _require_scope(actor, report, "report", C.VERIFY_FIELD_REPORT)
    if report.reporter_id == actor.pk:
        raise PermissionDenied("You cannot review your own report.")
    if report.status not in (RecordStatus.SUBMITTED, RecordStatus.UNDER_REVIEW):
        raise ValidationError({"status": f"A {report.status} report cannot be reviewed."})
    reason = reason.strip()
    if not accepted and not reason:
        raise ValidationError({"reason": "A reason is required to reject a report."})

    report.status = RecordStatus.VERIFIED if accepted else RecordStatus.REJECTED
    report.reviewed_by = actor
    report.reviewed_at = timezone.now()
    report.review_reason = reason
    _save(report)
    return report


# --- evidence ----------------------------------------------------------------

ALLOWED_EXTENSIONS = {
    EvidenceType.PHOTO: PHOTO_EXTENSIONS,
    EvidenceType.VIDEO: VIDEO_EXTENSIONS,
    EvidenceType.AUDIO: {".m4a", ".aac", ".mp3", ".wav", ".ogg", ".amr"},
    EvidenceType.DOCUMENT: {".pdf"},
    EvidenceType.RESULT_FORM: PHOTO_EXTENSIONS | {".pdf"},
}


def _max_bytes(evidence_type):
    megabytes = (
        settings.EVIDENCE_MAX_VIDEO_MB
        if evidence_type == EvidenceType.VIDEO
        else settings.EVIDENCE_MAX_UPLOAD_MB
    )
    return megabytes * 1024 * 1024


def can_add_evidence(actor, parent) -> bool:
    if parent.reporter_id == actor.pk:
        return True
    return scope_contains(actor, parent, permission=C.MANAGE_EVIDENCE)


@transaction.atomic
def add_evidence(
    *,
    actor,
    parent,
    upload,
    type,  # noqa: A002 - mirrors the client field name
    caption="",
    latitude=None,
    longitude=None,
    captured_at=None,
    content_hash="",
    client_id=None,
) -> Evidence:
    if client_id:
        existing = Evidence.objects.filter(client_id=client_id, uploader=actor).first()
        if existing is not None:
            return existing

    if not can_add_evidence(actor, parent):
        raise PermissionDenied("You cannot add evidence to this record.")
    if isinstance(parent, Incident) and parent.status == I.CLOSED:
        raise ValidationError("Evidence cannot be added to a closed incident.")

    allowed = ALLOWED_EXTENSIONS.get(type)
    if allowed is None:
        raise ValidationError({"type": "Upload a photo, video, audio, document or result form."})
    extension = Path(upload.name).suffix.lower()
    if extension not in allowed:
        raise ValidationError(
            {"file": f"{extension or 'This file'} is not accepted for {type} evidence."}
        )
    if upload.size == 0:
        raise ValidationError({"file": "The file is empty."})
    if upload.size > _max_bytes(type):
        raise ValidationError(
            {"file": f"The file exceeds the {_max_bytes(type) // (1024 * 1024)} MB limit."}
        )

    digest = hashlib.sha256()
    head = b""
    for chunk in upload.chunks():
        if len(head) < 16:
            head += chunk[: 16 - len(head)]
        digest.update(chunk)
    upload.seek(0)
    sha256 = digest.hexdigest()

    if not matches_signature(extension, head):
        raise ValidationError({"file": "The file content does not match its type."})
    if content_hash and content_hash.lower() != sha256:
        raise ValidationError(
            {"content_hash": "The file does not match the hash recorded on the device."}
        )

    evidence = Evidence(
        type=type,
        file=upload,
        file_name=Path(upload.name).name[:255],
        mime_type=mimetypes.guess_type(upload.name)[0] or "application/octet-stream",
        size=upload.size,
        content_hash=sha256,
        caption=caption.strip(),
        latitude=latitude,
        longitude=longitude,
        captured_at=captured_at,
        uploader=actor,
        client_id=client_id,
        incident=parent if isinstance(parent, Incident) else None,
        report=parent if isinstance(parent, FieldReport) else None,
        result=parent if parent._meta.label == "results.ResultSubmission" else None,
    )
    _save(evidence)
    if isinstance(parent, Incident):
        IncidentEvent.objects.create(
            incident=parent,
            action=IncidentEvent.Action.EVIDENCE_ADDED,
            note=f"{evidence.get_type_display()}: {evidence.file_name}",
            actor=actor,
        )
    return evidence
