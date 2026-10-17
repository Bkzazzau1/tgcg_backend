"""Projects and the people taking part in them. All changes go through here.

Only a National Administrator (manageProjects, protected) creates, changes
and cancels projects. Coordinators holding assignProjectParticipants assign
people with a role to a place inside both the project's areas and their own;
the person accepts or declines in their app, and may withdraw later. While a
project runs, an accepted participant's app shares their location without a
switch (see apps.tracking.services), and what they report counts for it.
"""

from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.access import audit, engine
from apps.access.services import authority_over_member
from apps.core.choices import ElectionStatus, MembershipState
from apps.core.permissions import Capability
from apps.geography.scope import SCOPE_FIELDS

from .models import (
    LIVE_PARTICIPATION,
    Participation,
    ParticipationStatus,
    Project,
    ProjectArea,
    ProjectStatus,
)

C = Capability
PS = ParticipationStatus
# The longest a project may run.
LONGEST = timedelta(days=31)
# Results captured during a project may still arrive this long after it ends
# (phones that were offline).
RESULTS_GRACE = timedelta(days=1)
# How long a finished project stays in people's lists.
RECENT = timedelta(days=7)


def overlaps(a, b) -> bool:
    """Whether two areas share ground: one lies inside the other."""
    return engine.area_contains(a, b) or engine.area_contains(b, a)


def can_manage(user) -> bool:
    return engine.allows(user, C.MANAGE_PROJECTS)


def _require_manager(actor):
    if not can_manage(actor):
        raise PermissionDenied("Only a National Administrator creates and changes projects.")


def _audit_project(actor, action, project, reason="", changes=None):
    audit.record(
        actor=actor,
        action=action,
        target_type="project",
        target_id=project.uuid,
        target_label=project.name,
        changes=changes,
        reason=reason,
    )


def _audit_participation(actor, action, participation, reason="", changes=None):
    member = participation.member
    audit.record(
        actor=actor,
        action=action,
        target_type="member",
        target_id=member.membership_number,
        target_label=member.full_name,
        changes={"project": str(participation.project.uuid), **(changes or {})},
        reason=reason,
    )


# --- projects --------------------------------------------------------------------


def _build_areas(areas) -> list[ProjectArea]:
    built, seen = [], set()
    for attrs in areas:
        area = ProjectArea(**{key: attrs.get(key) for key in ("scope_level", *SCOPE_FIELDS)})
        area.normalize_scope()
        field = area.scope_unit_field
        key = (area.scope_level, getattr(area, f"{field}_id") if field else None)
        if key in seen:
            raise ValidationError({"areas": "The same area is listed twice."})
        seen.add(key)
        built.append(area)
    if not built:
        raise ValidationError({"areas": "Choose at least one area."})
    return built


def _check_window(starts_at, ends_at):
    if ends_at <= starts_at:
        raise ValidationError({"ends_at": "The end must come after the start."})
    if ends_at - starts_at > LONGEST:
        raise ValidationError({"ends_at": "A project runs for at most 31 days."})


def _check_election(election, areas):
    if election is not None and not all(overlaps(area, election) for area in areas):
        raise ValidationError({"election": "This election is not held in the project's areas."})


@transaction.atomic
def create_project(
    *,
    actor,
    name: str,
    starts_at,
    ends_at,
    areas,
    description: str = "",
    election=None,
    check_place: bool = True,
) -> Project:
    _require_manager(actor)
    if starts_at < timezone.now() - timedelta(minutes=10):
        raise ValidationError({"starts_at": "Choose a start from now on."})
    _check_window(starts_at, ends_at)
    built = _build_areas(areas)
    _check_election(election, built)
    project = Project(
        name=" ".join((name or "").split()),
        description=(description or "").strip(),
        starts_at=starts_at,
        ends_at=ends_at,
        election=election,
        check_place=check_place,
        created_by=actor,
    )
    project.full_clean()
    project.save()
    for area in built:
        area.project = project
        area.save()
    _audit_project(
        actor,
        "project.created",
        project,
        changes={
            "starts_at": starts_at.isoformat(),
            "ends_at": ends_at.isoformat(),
            "areas": [str(area) for area in built],
            "election": election.code if election else None,
        },
    )
    return project


def _lock(project) -> Project:
    return Project.objects.select_for_update().get(pk=project.pk)


@transaction.atomic
def update_project(*, actor, project: Project, **fields) -> Project:
    """Change a project. Its start, areas and election change only before it
    starts; its name, brief, end and place check until it ends."""
    _require_manager(actor)
    project = _lock(project)
    status = project.status_at()
    if status in (ProjectStatus.ENDED, ProjectStatus.CANCELLED):
        raise ValidationError(f"This project has {status}.")
    started = status == ProjectStatus.ACTIVE
    for name in ("starts_at", "areas", "election"):
        if started and name in fields:
            raise ValidationError({name: "This cannot change once the project has started."})
    changed = []
    areas = fields.pop("areas", None)
    for name, value in fields.items():
        if name == "name":
            value = " ".join((value or "").split())
        elif name == "description":
            value = (value or "").strip()
        if getattr(project, name) != value:
            setattr(project, name, value)
            changed.append(name)
    if "starts_at" in changed and project.starts_at < timezone.now() - timedelta(minutes=10):
        raise ValidationError({"starts_at": "Choose a start from now on."})
    if "ends_at" in changed and project.ends_at <= timezone.now():
        raise ValidationError({"ends_at": "Choose an end in the future."})
    _check_window(project.starts_at, project.ends_at)
    built = _build_areas(areas) if areas is not None else list(project.areas.all())
    _check_election(project.election, built)
    project.full_clean()
    project.save()
    if areas is not None:
        project.areas.all().delete()
        for area in built:
            area.project = project
            area.save()
        changed.append("areas")
    if changed:
        _audit_project(actor, "project.updated", project, changes={"fields": sorted(changed)})
    return project


@transaction.atomic
def cancel_project(*, actor, project: Project, reason: str) -> Project:
    _require_manager(actor)
    project = _lock(project)
    status = project.status_at()
    if status in (ProjectStatus.ENDED, ProjectStatus.CANCELLED):
        raise ValidationError(f"This project has {status}.")
    if not (reason or "").strip():
        raise ValidationError({"reason": "Say why the project is cancelled."})
    project.cancelled_at = timezone.now()
    project.cancelled_by = actor
    project.cancel_reason = reason.strip()
    project.save(update_fields=["cancelled_at", "cancelled_by", "cancel_reason", "updated_at"])
    from apps.tracking.services import end_project_duty

    end_project_duty(project=project, at=project.cancelled_at)
    _audit_project(actor, "project.cancelled", project, reason=reason.strip())
    return project


def sees(user, project) -> bool:
    """Everyone with a role in one of the project's areas, and whoever manages projects."""
    if can_manage(user):
        return True
    grants = engine.active_grants(user)
    areas = list(project.areas.all())
    return any(overlaps(area, mine) for grant in grants for mine in grant.areas for area in areas)


def visible_projects(user, now=None) -> list[Project]:
    """Projects that are scheduled, on now, or finished within the last week,
    for someone with a role in their areas (all of them for a manager)."""
    now = now or timezone.now()
    candidates = (
        Project.objects.filter(ends_at__gt=now - RECENT)
        .select_related("election")
        .prefetch_related("areas")
        .order_by("starts_at", "id")
    )
    if can_manage(user):
        return list(candidates)
    grants = engine.active_grants(user)
    if not grants:
        return []
    mine = [area for grant in grants for area in grant.areas]
    return [
        project
        for project in candidates
        if any(overlaps(area, own) for area in project.areas.all() for own in mine)
    ]


def require_visible(user, project):
    if not sees(user, project):
        raise PermissionDenied("This project is outside your areas.")


# --- results ---------------------------------------------------------------------


def accepting_results(election, at=None, now=None) -> bool:
    """An election named by a project takes results only while one of its
    projects runs (or, from phones that were offline, captured then and sent
    within a day); any other election, while its status is open."""
    now = now or timezone.now()
    at = min(at or now, now)
    linked = Project.objects.filter(election=election)
    if not linked.exists():
        return election.status == ElectionStatus.OPEN
    return (
        linked.filter(starts_at__lte=at, ends_at__gt=max(at, now - RESULTS_GRACE))
        .filter(Q(cancelled_at__isnull=True) | Q(cancelled_at__gt=at))
        .exists()
    )


# --- taking part -----------------------------------------------------------------


def current_participation(user, at=None) -> Participation | None:
    """The user's accepted place in a project running at `at` (default now)."""
    at = at or timezone.now()
    member = getattr(user, "member", None) if user is not None else None
    if member is None:
        return None
    return (
        Participation.objects.filter(
            member=member,
            status=PS.ACCEPTED,
            project__starts_at__lte=at,
            project__ends_at__gt=at,
        )
        .filter(Q(project__cancelled_at__isnull=True) | Q(project__cancelled_at__gt=at))
        .select_related("project", "polling_unit")
        .order_by("project__starts_at")
        .first()
    )


def current_project(user, at=None) -> Project | None:
    participation = current_participation(user, at)
    return participation.project if participation else None


def _place(place) -> Participation:
    area = Participation(**{key: place.get(key) for key in ("scope_level", *SCOPE_FIELDS)})
    area.normalize_scope()
    return area


@transaction.atomic
def assign(*, actor, project: Project, member, place: dict) -> Participation:
    """A coordinator puts someone with a role into a project, at a place
    inside both the project's areas and the coordinator's own."""
    from apps.membership.models import Member

    project = _lock(project)
    status = project.status_at()
    if status in (ProjectStatus.ENDED, ProjectStatus.CANCELLED):
        raise ValidationError(f"This project has {status}.")
    member = Member.objects.select_related("user").get(pk=member.pk)
    participation = _place(place)
    if not any(engine.area_contains(area, participation) for area in project.areas.all()):
        raise ValidationError({"place": "This place is outside the project's areas."})
    authority_over_member(
        actor, C.ASSIGN_PROJECT_PARTICIPANTS, member, area=participation, what="place"
    )
    if member.membership_state != MembershipState.ACTIVE:
        raise ValidationError({"member": "This membership is not active."})
    if member.user_id is None or not engine.active_grants(member.user):
        raise ValidationError({"member": "Only someone with a role takes part in a project."})
    live = Participation.objects.filter(member=member, status__in=LIVE_PARTICIPATION)
    if live.filter(project=project).exists():
        raise ValidationError({"member": "They are already in this project."})
    clash = live.filter(
        project__starts_at__lt=project.ends_at,
        project__ends_at__gt=project.starts_at,
        project__cancelled_at__isnull=True,
    )
    if clash.exists():
        raise ValidationError({"member": "They are in another project at the same time."})
    participation.project = project
    participation.member = member
    participation.assigned_by = actor
    participation.full_clean()
    try:
        with transaction.atomic():
            participation.save()
    except IntegrityError as exc:
        raise ValidationError({"member": "They are already in this project."}) from exc
    _audit_participation(
        actor,
        "project.participantAssigned",
        participation,
        changes={"place": str(_label(participation))},
    )
    return participation


def _label(area):
    field = area.scope_unit_field
    return "Nigeria" if field is None else getattr(area, field)


def _lock_participation(participation) -> Participation:
    return (
        Participation.objects.select_for_update(of=("self",))
        .select_related("project", "member__user")
        .get(pk=participation.pk)
    )


def _require_own(user, participation):
    if participation.member.user_id != user.pk:
        raise PermissionDenied("This is someone else's place in a project.")


def _require_running(participation):
    status = participation.project.status_at()
    if status in (ProjectStatus.ENDED, ProjectStatus.CANCELLED):
        raise ValidationError(f"This project has {status}.")


@transaction.atomic
def respond(*, user, participation: Participation, accept: bool, reason: str = ""):
    """The person agrees to take part, and to sharing their location while it
    runs, or declines."""
    participation = _lock_participation(participation)
    _require_own(user, participation)
    _require_running(participation)
    if participation.status != PS.INVITED:
        raise ValidationError("You have already answered.")
    participation.status = PS.ACCEPTED if accept else PS.DECLINED
    participation.responded_at = timezone.now()
    participation.reason = (reason or "").strip()
    participation.save(update_fields=["status", "responded_at", "reason", "updated_at"])
    action = "project.participantAccepted" if accept else "project.participantDeclined"
    _audit_participation(user, action, participation, reason=participation.reason)
    return participation


def _end(participation, status, by, reason):
    from apps.tracking.services import end_participation_duty

    now = timezone.now()
    participation.status = status
    participation.ended_at = now
    participation.ended_by = by
    participation.reason = (reason or "").strip()
    participation.save(update_fields=["status", "ended_at", "ended_by", "reason", "updated_at"])
    end_participation_duty(participation=participation, at=now)


@transaction.atomic
def withdraw(*, user, participation: Participation, reason: str = "") -> Participation:
    """The person stops taking part: their location is no longer shared."""
    participation = _lock_participation(participation)
    _require_own(user, participation)
    if participation.status != PS.ACCEPTED:
        raise ValidationError("You are not taking part in this project.")
    _end(participation, PS.WITHDRAWN, user, reason)
    _audit_participation(user, "project.participantWithdrew", participation, reason=reason)
    return participation


@transaction.atomic
def remove(*, actor, participation: Participation, reason: str) -> Participation:
    """A coordinator over the person's place takes them out of the project."""
    participation = _lock_participation(participation)
    authority_over_member(
        actor,
        C.ASSIGN_PROJECT_PARTICIPANTS,
        participation.member,
        area=participation,
        what="place",
    )
    if participation.status not in LIVE_PARTICIPATION:
        raise ValidationError(f"They have already {participation.status}.")
    if not (reason or "").strip():
        raise ValidationError({"reason": "Say why they are taken out of the project."})
    _end(participation, PS.REMOVED, actor, reason)
    _audit_participation(actor, "project.participantRemoved", participation, reason=reason.strip())
    return participation
