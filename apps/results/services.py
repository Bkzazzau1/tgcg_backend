"""Result submission, integrity checks and review. Mirrors the client's
ResultIntegrityPolicy (lib/tgcg/results/result_integrity.dart)."""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.core.choices import GeographyLevel, RecordStatus, SubmissionSource
from apps.core.models import Sequence
from apps.core.permissions import Capability, has_capability
from apps.geography.gps import LocationStatus
from apps.geography.scope import scope_contains
from apps.projects.services import accepting_results, current_project

from .models import ACTIVE_RESULT_STATUSES, PartyVote, ResultEvent, ResultSubmission

C = Capability
R = RecordStatus

VERIFIABLE_FROM = {R.SUBMITTED, R.UNDER_REVIEW, R.DISPUTED}
DISPUTABLE_FROM = {R.SUBMITTED, R.UNDER_REVIEW, R.VERIFIED}
REJECTABLE_FROM = {R.SUBMITTED, R.UNDER_REVIEW, R.DISPUTED}


def _event(submission, previous, actor, note=""):
    ResultEvent.objects.create(
        submission=submission,
        from_status=previous or "",
        to_status=submission.status,
        note=note,
        actor=actor,
    )


def _save(submission):
    submission.full_clean(exclude=["reference"] if not submission.reference else None)
    try:
        with transaction.atomic():
            submission.save()
    except IntegrityError as exc:
        raise ValidationError("Another result for this polling unit is already verified.") from exc


def check_integrity(
    *,
    party_votes: dict[str, int],
    total_valid_votes: int,
    accredited_voters: int,
    rejected_votes: int | None,
    registered_voters: int | None,
    ocr_party_votes: dict[str, int] | None,
) -> tuple[bool, bool | None, list[str]]:
    """Return (arithmetic_valid, ocr_matched_manual_entry, notes)."""
    notes = []
    arithmetic_valid = True

    party_sum = sum(party_votes.values())
    if party_sum != total_valid_votes:
        arithmetic_valid = False
        notes.append(
            f"Party vote sum ({party_sum}) does not match the recorded total ({total_valid_votes})."
        )
    ballots = total_valid_votes + (rejected_votes or 0)
    if ballots > accredited_voters:
        arithmetic_valid = False
        notes.append(
            f"Valid plus rejected votes ({ballots}) exceed accredited voters ({accredited_voters})."
        )
    if registered_voters is not None and accredited_voters > registered_voters:
        arithmetic_valid = False
        notes.append(
            f"Accredited voters ({accredited_voters}) exceed registered voters "
            f"({registered_voters})."
        )

    ocr_matched = None
    if ocr_party_votes is not None:
        ocr_matched = {k: v for k, v in ocr_party_votes.items() if v} == {
            k: v for k, v in party_votes.items() if v
        }
        if not ocr_matched:
            notes.append(
                "OCR-extracted party figures differ from the submitted figures; "
                "human verification is required."
            )
    return arithmetic_valid, ocr_matched, notes


@transaction.atomic
def submit_result(
    *,
    actor,
    election,
    polling_unit,
    party_votes: dict[str, int],
    accredited_voters: int,
    total_valid_votes: int,
    source: str,
    rejected_votes=None,
    registered_voters=None,
    submitted_at=None,
    source_reference="",
    client_id=None,
    origin=None,
    ocr_party_votes=None,
    ocr_confidence=None,
    gps=None,
) -> ResultSubmission:
    if client_id:
        existing = ResultSubmission.objects.filter(client_id=client_id, submitted_by=actor).first()
        if existing is not None:
            return existing  # offline retry

    submitted_at = submitted_at or timezone.now()
    if not accepting_results(election, at=submitted_at):
        raise ValidationError({"election": "This election is not open for results."})
    if source == SubmissionSource.APP and not gps:
        raise ValidationError({"gps": "Turn on location: results from the app need a GPS reading."})

    submission = ResultSubmission(
        election=election,
        submitted_by=actor,
        submitted_at=submitted_at,
        project=current_project(actor, min(submitted_at, timezone.now())),
        source=source,
        source_reference=source_reference.strip(),
        client_id=client_id,
        scope_level=GeographyLevel.POLLING_UNIT,
        polling_unit=polling_unit,
        accredited_voters=accredited_voters,
        total_valid_votes=total_valid_votes,
        rejected_votes=rejected_votes,
    )
    if origin:
        submission.origin = origin
    submission.normalize_scope()
    if not scope_contains(election, submission):
        raise ValidationError({"polling_unit": "This polling unit is not part of the election."})
    if not scope_contains(actor, submission, permission=C.SUBMIT_ELECTION_RESULT):
        raise PermissionDenied("The polling unit is outside your operational scope.")

    parties = {party.acronym: party for party in election.parties.all()}
    unknown = sorted(set(party_votes) - set(parties))
    if unknown:
        raise ValidationError(
            {"party_votes": f"Not on the ballot for this election: {', '.join(unknown)}."}
        )
    votes = {acronym: int(party_votes.get(acronym, 0)) for acronym in parties}

    notes = []
    master_registered = polling_unit.registered_voters
    if master_registered is not None:
        if registered_voters is not None and registered_voters != master_registered:
            notes.append(
                f"Sheet shows {registered_voters} registered voters; master data has "
                f"{master_registered}. Master data used."
            )
        registered_voters = master_registered
    submission.registered_voters = registered_voters
    submission.apply_fix(gps, polling_unit)
    if submission.location_status == LocationStatus.AWAY:
        notes.append(
            f"Submitted {submission.distance_to_pu_m} m from the polling unit's known location."
        )
    elif submission.location_status == LocationStatus.SUSPECT:
        notes.append("The phone reported a mock-location app: the GPS may be faked.")

    arithmetic_valid, ocr_matched, integrity_notes = check_integrity(
        party_votes=votes,
        total_valid_votes=total_valid_votes,
        accredited_voters=accredited_voters,
        rejected_votes=rejected_votes,
        registered_voters=registered_voters,
        ocr_party_votes=ocr_party_votes,
    )
    duplicate = ResultSubmission.objects.filter(
        election=election, polling_unit=polling_unit, status__in=ACTIVE_RESULT_STATUSES
    ).exists()
    agent_matched = (
        actor.scope_level == GeographyLevel.POLLING_UNIT
        and actor.polling_unit_id == polling_unit.pk
    )
    if duplicate:
        integrity_notes.append("A possible duplicate or conflicting submission was detected.")
    if not agent_matched:
        integrity_notes.append("Submitted by someone other than the polling unit's own agent.")

    submission.arithmetic_valid = arithmetic_valid
    submission.duplicate_suspected = duplicate
    submission.agent_scope_matched = agent_matched
    submission.ocr_confidence = ocr_confidence
    submission.ocr_matched_manual_entry = ocr_matched
    submission.validation_notes = notes + integrity_notes
    submission.status = R.UNDER_REVIEW if submission.requires_human_review else R.SUBMITTED
    submission.reference = f"RES-{Sequence.next_value('result'):06d}"
    _save(submission)
    PartyVote.objects.bulk_create(
        PartyVote(submission=submission, party=parties[acronym], votes=count)
        for acronym, count in votes.items()
    )
    _event(submission, None, actor, "Submitted")
    return submission


def _lock_for_review(actor, submission, capability):
    submission = ResultSubmission.objects.select_for_update().get(pk=submission.pk)
    if not scope_contains(actor, submission, permission=capability):
        if has_capability(actor, capability):
            raise PermissionDenied("The result is outside your operational scope.")
        raise PermissionDenied("Your role does not permit this action.")
    return submission


@transaction.atomic
def verify_result(*, actor, submission, note="") -> ResultSubmission:
    submission = _lock_for_review(actor, submission, C.VERIFY_ELECTION_RESULT)
    if submission.submitted_by_id == actor.pk:
        raise PermissionDenied("You cannot verify a result you submitted.")
    if submission.status not in VERIFIABLE_FROM:
        raise ValidationError({"status": f"A {submission.status} result cannot be verified."})
    if not submission.arithmetic_valid:
        raise ValidationError(
            {"status": "Results that fail arithmetic checks cannot be verified; resubmit."}
        )
    previous = submission.status
    submission.status = R.VERIFIED
    submission.verified_by = actor
    submission.verified_at = timezone.now()
    submission.review_reason = ""
    _save(submission)
    _event(submission, previous, actor, note.strip())
    return submission


@transaction.atomic
def dispute_result(*, actor, submission, reason) -> ResultSubmission:
    submission = _lock_for_review(actor, submission, C.DISPUTE_ELECTION_RESULT)
    reason = reason.strip()
    if not reason:
        raise ValidationError({"reason": "A reason is required to dispute a result."})
    if submission.status not in DISPUTABLE_FROM:
        raise ValidationError({"status": f"A {submission.status} result cannot be disputed."})
    previous = submission.status
    submission.status = R.DISPUTED
    submission.review_reason = reason
    _save(submission)
    _event(submission, previous, actor, reason)
    return submission


@transaction.atomic
def reject_result(*, actor, submission, reason) -> ResultSubmission:
    submission = _lock_for_review(actor, submission, C.VERIFY_ELECTION_RESULT)
    reason = reason.strip()
    if not reason:
        raise ValidationError({"reason": "A reason is required to reject a result."})
    if submission.status not in REJECTABLE_FROM:
        raise ValidationError({"status": f"A {submission.status} result cannot be rejected."})
    previous = submission.status
    submission.status = R.REJECTED
    submission.review_reason = reason
    _save(submission)
    _event(submission, previous, actor, reason)
    return submission
