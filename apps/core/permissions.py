"""Role capabilities, ported from the Flutter client's TgcgPermissionPolicy
(lib/tgcg/domain/permissions.dart). Keep the two in sync.
"""

from django.db import models
from rest_framework.permissions import BasePermission

from .choices import Role


class Capability(models.TextChoices):
    VIEW_NATIONAL_DASHBOARD = "viewNationalDashboard"
    VIEW_SITUATION_ROOM = "viewSituationRoom"
    # Screens the role grid (docs/access-control.md, "Role menus") gives to
    # different groups, so each has its own permission.
    VIEW_LIVE_OPERATIONS = "viewLiveOperations"
    VIEW_ALERTS = "viewAlerts"
    VIEW_ANALYTICS = "viewAnalytics"
    VIEW_GEOGRAPHY = "viewGeography"
    MANAGE_GEOGRAPHY = "manageGeography"
    MANAGE_MEMBERSHIP = "manageMembership"
    REGISTER_MEMBERS = "registerMembers"
    CONFIRM_AGENT_LOCATION = "confirmAgentLocation"
    ACCREDIT_AGENTS = "accreditAgents"
    MANAGE_AGENT_ASSIGNMENTS = "manageAgentAssignments"
    VIEW_INCIDENTS = "viewIncidents"
    CREATE_INCIDENT = "createIncident"
    ACKNOWLEDGE_INCIDENT = "acknowledgeIncident"
    ASSIGN_INCIDENT = "assignIncident"
    CLOSE_INCIDENT = "closeIncident"
    SUBMIT_FIELD_REPORT = "submitFieldReport"
    VERIFY_FIELD_REPORT = "verifyFieldReport"
    SUBMIT_ELECTION_RESULT = "submitElectionResult"
    VERIFY_ELECTION_RESULT = "verifyElectionResult"
    DISPUTE_ELECTION_RESULT = "disputeElectionResult"
    VIEW_COLLATION = "viewCollation"
    MANAGE_COLLATION = "manageCollation"
    VIEW_COMMUNICATIONS = "viewCommunications"
    SEND_OPERATIONAL_MESSAGE = "sendOperationalMessage"
    SEND_BROADCAST = "sendBroadcast"
    VIEW_MEDIA_INTELLIGENCE = "viewMediaIntelligence"
    VIEW_DISCUSSION_ROOM = "viewDiscussionRoom"
    CREATE_DISCUSSION_THREAD = "createDiscussionThread"
    POST_DISCUSSION_REPLY = "postDiscussionReply"
    MODERATE_DISCUSSION = "moderateDiscussion"
    VIEW_MEETING_ROOM = "viewMeetingRoom"
    START_MEETING = "startMeeting"
    JOIN_MEETING = "joinMeeting"
    VIEW_EVIDENCE = "viewEvidence"
    MANAGE_EVIDENCE = "manageEvidence"
    VIEW_AUDIT = "viewAudit"
    EXPORT_REPORTS = "exportReports"
    MANAGE_USERS = "manageUsers"
    MANAGE_SYSTEM_SETTINGS = "manageSystemSettings"
    # Roles, appointments and delegation (apps.access).
    VIEW_ROLES = "viewRoles"
    MANAGE_ROLES = "manageRoles"
    ASSIGN_ROLES = "assignRoles"
    MANAGE_ASSIGNMENTS = "manageAssignments"
    DELEGATE_AUTHORITY = "delegateAuthority"
    # Membership administration.
    VIEW_MEMBER_SENSITIVE_DATA = "viewMemberSensitiveData"
    VERIFY_MEMBER_IDENTITY = "verifyMemberIdentity"
    SUSPEND_MEMBERS = "suspendMembers"
    REVIEW_POLLING_UNIT_CHANGES = "reviewPollingUnitChanges"
    # Protected: only National Administrators, never delegable.
    APPOINT_NATIONAL_ADMINISTRATOR = "appointNationalAdministrator"
    APPROVE_ADMINISTRATOR_REMOVAL = "approveAdministratorRemoval"


C = Capability

# Agents and observers in the field. Their accounts act only for a member whose
# identity a coordinator has verified; plain membership needs no verification.
FIELD_ROLES = frozenset({Role.POLLING_UNIT_AGENT, Role.OBSERVER})

_COMMON_DISCUSSION = {C.VIEW_DISCUSSION_ROOM, C.CREATE_DISCUSSION_THREAD, C.POST_DISCUSSION_REPLY}
_COMMON_MEETING = {C.VIEW_MEETING_ROOM, C.START_MEETING, C.JOIN_MEETING}

ROLE_CAPABILITIES: dict[str, frozenset[str]] = {
    Role.NATIONAL_ADMINISTRATOR: frozenset(Capability.values),
    Role.NATIONAL_COLLATION_OFFICER: frozenset(
        {
            C.VIEW_NATIONAL_DASHBOARD,
            C.VIEW_SITUATION_ROOM,
            C.VIEW_LIVE_OPERATIONS,
            C.VIEW_ALERTS,
            C.VIEW_GEOGRAPHY,
            C.VIEW_INCIDENTS,
            C.VERIFY_FIELD_REPORT,
            C.VERIFY_ELECTION_RESULT,
            C.DISPUTE_ELECTION_RESULT,
            C.VIEW_COLLATION,
            C.MANAGE_COLLATION,
            C.VIEW_COMMUNICATIONS,
            C.SEND_OPERATIONAL_MESSAGE,
            *_COMMON_DISCUSSION,
            C.VIEW_MEETING_ROOM,
            C.JOIN_MEETING,
            C.VIEW_EVIDENCE,
            C.EXPORT_REPORTS,
        }
    ),
    Role.SITUATION_ROOM_DIRECTOR: frozenset(
        {
            C.VIEW_NATIONAL_DASHBOARD,
            C.VIEW_SITUATION_ROOM,
            C.VIEW_LIVE_OPERATIONS,
            C.VIEW_ALERTS,
            C.VIEW_ANALYTICS,
            C.VIEW_GEOGRAPHY,
            C.VIEW_INCIDENTS,
            C.CREATE_INCIDENT,
            C.ACKNOWLEDGE_INCIDENT,
            C.ASSIGN_INCIDENT,
            C.CLOSE_INCIDENT,
            C.VERIFY_FIELD_REPORT,
            C.VERIFY_ELECTION_RESULT,
            C.DISPUTE_ELECTION_RESULT,
            C.VIEW_COLLATION,
            C.VIEW_COMMUNICATIONS,
            C.SEND_OPERATIONAL_MESSAGE,
            C.SEND_BROADCAST,
            C.VIEW_MEDIA_INTELLIGENCE,
            *_COMMON_DISCUSSION,
            *_COMMON_MEETING,
            C.VIEW_EVIDENCE,
            C.MANAGE_EVIDENCE,
            C.EXPORT_REPORTS,
        }
    ),
    # Coordinators see no Situation Room: it is national (15 October 2026), for
    # the national roles and whoever an administrator gives it through a role.
    Role.ZONAL_COORDINATOR: frozenset(
        {
            C.VIEW_LIVE_OPERATIONS,
            C.VIEW_ALERTS,
            C.VIEW_ANALYTICS,
            C.VIEW_GEOGRAPHY,
            C.VIEW_INCIDENTS,
            C.CREATE_INCIDENT,
            C.ACKNOWLEDGE_INCIDENT,
            C.ASSIGN_INCIDENT,
            C.SUBMIT_FIELD_REPORT,
            C.VERIFY_FIELD_REPORT,
            C.VERIFY_ELECTION_RESULT,
            C.VIEW_COLLATION,
            C.VIEW_COMMUNICATIONS,
            C.SEND_OPERATIONAL_MESSAGE,
            C.SEND_BROADCAST,
            C.VIEW_MEDIA_INTELLIGENCE,
            *_COMMON_DISCUSSION,
            *_COMMON_MEETING,
            C.VIEW_EVIDENCE,
            C.EXPORT_REPORTS,
        }
    ),
    Role.STATE_COORDINATOR: frozenset(
        {
            C.VIEW_LIVE_OPERATIONS,
            C.VIEW_ALERTS,
            C.VIEW_ANALYTICS,
            C.VIEW_GEOGRAPHY,
            C.ACCREDIT_AGENTS,
            C.MANAGE_AGENT_ASSIGNMENTS,
            C.VIEW_INCIDENTS,
            C.CREATE_INCIDENT,
            C.ACKNOWLEDGE_INCIDENT,
            C.ASSIGN_INCIDENT,
            C.SUBMIT_FIELD_REPORT,
            C.VERIFY_FIELD_REPORT,
            C.VERIFY_ELECTION_RESULT,
            C.DISPUTE_ELECTION_RESULT,
            C.VIEW_COLLATION,
            C.VIEW_COMMUNICATIONS,
            C.SEND_OPERATIONAL_MESSAGE,
            C.SEND_BROADCAST,
            C.VIEW_MEDIA_INTELLIGENCE,
            *_COMMON_DISCUSSION,
            *_COMMON_MEETING,
            C.VIEW_EVIDENCE,
            C.EXPORT_REPORTS,
        }
    ),
    Role.LGA_COORDINATOR: frozenset(
        {
            C.VIEW_LIVE_OPERATIONS,
            C.VIEW_ALERTS,
            C.VIEW_GEOGRAPHY,
            C.MANAGE_AGENT_ASSIGNMENTS,
            C.VIEW_INCIDENTS,
            C.CREATE_INCIDENT,
            C.ACKNOWLEDGE_INCIDENT,
            C.ASSIGN_INCIDENT,
            C.SUBMIT_FIELD_REPORT,
            C.VERIFY_FIELD_REPORT,
            C.SUBMIT_ELECTION_RESULT,
            C.VERIFY_ELECTION_RESULT,
            C.VIEW_COLLATION,
            C.VIEW_COMMUNICATIONS,
            C.SEND_OPERATIONAL_MESSAGE,
            *_COMMON_DISCUSSION,
            *_COMMON_MEETING,
            C.VIEW_EVIDENCE,
        }
    ),
    Role.WARD_COORDINATOR: frozenset(
        {
            C.VIEW_LIVE_OPERATIONS,
            C.VIEW_GEOGRAPHY,
            C.VIEW_INCIDENTS,
            C.CREATE_INCIDENT,
            C.ACKNOWLEDGE_INCIDENT,
            C.SUBMIT_FIELD_REPORT,
            C.SUBMIT_ELECTION_RESULT,
            C.VIEW_COLLATION,
            C.VIEW_COMMUNICATIONS,
            C.SEND_OPERATIONAL_MESSAGE,
            *_COMMON_DISCUSSION,
            # Posts from the ward's polling units wait for this approval.
            C.MODERATE_DISCUSSION,
            *_COMMON_MEETING,
            C.VIEW_EVIDENCE,
        }
    ),
    Role.POLLING_UNIT_AGENT: frozenset(
        {
            C.VIEW_GEOGRAPHY,
            C.CREATE_INCIDENT,
            C.SUBMIT_FIELD_REPORT,
            C.SUBMIT_ELECTION_RESULT,
            C.VIEW_COMMUNICATIONS,
            C.SEND_OPERATIONAL_MESSAGE,
            C.VIEW_DISCUSSION_ROOM,
            C.CREATE_DISCUSSION_THREAD,
            C.POST_DISCUSSION_REPLY,
            C.VIEW_MEETING_ROOM,
            C.JOIN_MEETING,
            C.VIEW_EVIDENCE,
        }
    ),
    Role.OBSERVER: frozenset(
        {
            C.VIEW_GEOGRAPHY,
            C.CREATE_INCIDENT,
            C.SUBMIT_FIELD_REPORT,
            C.VIEW_COMMUNICATIONS,
            C.SEND_OPERATIONAL_MESSAGE,
            C.VIEW_DISCUSSION_ROOM,
            C.CREATE_DISCUSSION_THREAD,
            C.POST_DISCUSSION_REPLY,
            C.VIEW_MEETING_ROOM,
            C.JOIN_MEETING,
            C.VIEW_EVIDENCE,
        }
    ),
    Role.LEGAL_OFFICER: frozenset(
        {
            C.VIEW_SITUATION_ROOM,
            C.VIEW_GEOGRAPHY,
            C.VIEW_INCIDENTS,
            C.CREATE_INCIDENT,
            C.ACKNOWLEDGE_INCIDENT,
            C.DISPUTE_ELECTION_RESULT,
            C.VIEW_COLLATION,
            C.VIEW_COMMUNICATIONS,
            C.SEND_OPERATIONAL_MESSAGE,
            C.VIEW_MEDIA_INTELLIGENCE,
            *_COMMON_DISCUSSION,
            *_COMMON_MEETING,
            C.VIEW_EVIDENCE,
            C.MANAGE_EVIDENCE,
            C.VIEW_AUDIT,
            C.EXPORT_REPORTS,
        }
    ),
    # No election data: users, devices, sync and system health only.
    Role.TECHNICAL_SUPPORT: frozenset(
        {
            C.VIEW_GEOGRAPHY,
            C.VIEW_COMMUNICATIONS,
            C.SEND_OPERATIONAL_MESSAGE,
            *_COMMON_DISCUSSION,
            *_COMMON_MEETING,
            C.VIEW_AUDIT,
            C.MANAGE_USERS,
            C.MANAGE_SYSTEM_SETTINGS,
        }
    ),
    # Summaries: no raw evidence, forum or audit log.
    Role.READ_ONLY_EXECUTIVE: frozenset(
        {
            C.VIEW_NATIONAL_DASHBOARD,
            C.VIEW_SITUATION_ROOM,
            C.VIEW_LIVE_OPERATIONS,
            C.VIEW_ANALYTICS,
            C.VIEW_GEOGRAPHY,
            C.VIEW_INCIDENTS,
            C.VIEW_COLLATION,
            C.VIEW_MEDIA_INTELLIGENCE,
            C.VIEW_MEETING_ROOM,
            C.JOIN_MEETING,
            C.EXPORT_REPORTS,
        }
    ),
}


# Coordinators register members in person (with or without a PVC).
for _role in (
    Role.ZONAL_COORDINATOR,
    Role.STATE_COORDINATOR,
    Role.LGA_COORDINATOR,
    Role.WARD_COORDINATOR,
):
    ROLE_CAPABILITIES[_role] = ROLE_CAPABILITIES[_role] | {
        C.REGISTER_MEMBERS,
        # They saw members' contact details before those needed this permission.
        C.VIEW_MEMBER_SENSITIVE_DATA,
        # Confirming an agent's check-in also teaches the polling unit's location.
        C.CONFIRM_AGENT_LOCATION,
    }

# State, LGA and ward coordinators confirm the identities of members in their
# area, so the agents and role holders there can start (13 October 2026).
for _role in (Role.STATE_COORDINATOR, Role.LGA_COORDINATOR, Role.WARD_COORDINATOR):
    ROLE_CAPABILITIES[_role] = ROLE_CAPABILITIES[_role] | {C.VERIFY_MEMBER_IDENTITY}

# Members use only their own member-home endpoints; they hold no staff capability.
ROLE_CAPABILITIES[Role.MEMBER] = frozenset()


def is_staff_operator(user) -> bool:
    """Authenticated account holding at least one operational permission."""
    return bool(capabilities_for(user))


def capabilities_for(user) -> frozenset[str]:
    """Every permission the user holds somewhere (apps.access.engine).

    Use this only for coarse checks ("may use this endpoint at all"); checks
    on a resource in an area go through the engine with that area.
    """
    if not getattr(user, "is_authenticated", False) or not user.is_active:
        return frozenset()
    from apps.access.engine import permissions_for

    return permissions_for(user)


def has_capability(user, *capabilities: str) -> bool:
    """True when the user holds any of the given capabilities."""
    held = capabilities_for(user)
    return any(capability in held for capability in capabilities)


def requires(*capabilities: str, safe: tuple[str, ...] | None = None):
    """DRF permission: `capabilities` for writes; `safe` (default: same) for reads."""
    read_caps = safe if safe is not None else capabilities

    class _Capability(BasePermission):
        message = "Your role does not permit this action."

        def has_permission(self, request, view):
            needed = read_caps if request.method in ("GET", "HEAD", "OPTIONS") else capabilities
            return has_capability(request.user, *needed)

    _Capability.__name__ = f"Requires_{'_'.join(capabilities)}"
    return _Capability
