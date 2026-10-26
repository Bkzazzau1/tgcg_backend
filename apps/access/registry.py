"""The permission registry: what each permission code means.

Codes are the `Capability` values used across the backend and mirrored by the
Flutter client. Role definitions may only hold registered codes; anything
else is rejected, and an unknown code never authorizes anything.
"""

from dataclasses import dataclass

from apps.core.permissions import Capability

C = Capability


class Module:
    MEMBERSHIP = "membership"
    IDENTITY_VERIFICATION = "identityVerification"
    ROLE_MANAGEMENT = "roleManagement"
    GEOGRAPHY = "geography"
    POLLING_UNIT_MAPPING = "pollingUnitMapping"
    ASSIGNMENTS = "assignments"
    FIELD_MONITORING = "fieldMonitoring"
    INCIDENTS = "incidents"
    EVIDENCE = "evidence"
    RESULT_SUBMISSION = "resultSubmission"
    RESULT_VERIFICATION = "resultVerification"
    COLLATION = "collation"
    SITUATION_ROOM = "situationRoom"
    COMMUNICATIONS = "communications"
    DISCUSSION_ROOMS = "discussionRooms"
    MEETINGS = "meetings"
    MEDIA_INTELLIGENCE = "mediaIntelligence"
    ANALYTICS = "analytics"
    REPORTS = "reports"
    GOVERNANCE = "governance"
    SYSTEM_MONITORING = "systemMonitoring"
    WALLETS = "wallets"
    PROJECTS = "projects"
    ASK_ME = "askMe"


@dataclass(frozen=True)
class PermissionInfo:
    code: str
    module: str
    action: str
    label: str
    # Only National Administrators hold it; no role, grant or restriction can.
    protected: bool = False
    # Exposes personal data beyond what geographic visibility allows.
    sensitive: bool = False


def _p(code, module, action, label, **flags):
    return PermissionInfo(str(code), module, action, label, **flags)


M = Module

PERMISSIONS: dict[str, PermissionInfo] = {
    info.code: info
    for info in (
        _p(C.VIEW_NATIONAL_DASHBOARD, M.ANALYTICS, "view", "View the national dashboard"),
        _p(C.VIEW_SITUATION_ROOM, M.SITUATION_ROOM, "view", "View the situation room"),
        _p(C.VIEW_LIVE_OPERATIONS, M.SITUATION_ROOM, "view", "View live operations and agents"),
        _p(C.VIEW_ALERTS, M.SITUATION_ROOM, "view", "View the alert centre"),
        _p(C.VIEW_ANALYTICS, M.ANALYTICS, "view", "View data analytics"),
        _p(C.VIEW_GEOGRAPHY, M.GEOGRAPHY, "view", "View geography"),
        _p(C.MANAGE_GEOGRAPHY, M.GEOGRAPHY, "manage", "Manage geography master data"),
        _p(C.MANAGE_MEMBERSHIP, M.MEMBERSHIP, "manage", "Manage members"),
        _p(C.REGISTER_MEMBERS, M.MEMBERSHIP, "create", "Register members"),
        _p(
            C.VIEW_MEMBER_SENSITIVE_DATA,
            M.MEMBERSHIP,
            "view",
            "View members' personal data",
            sensitive=True,
        ),
        _p(C.SUSPEND_MEMBERS, M.MEMBERSHIP, "suspend", "Suspend or deactivate members"),
        _p(C.REVIEW_POLLING_UNIT_CHANGES, M.MEMBERSHIP, "approve", "Review polling unit changes"),
        _p(
            C.VERIFY_MEMBER_IDENTITY,
            M.IDENTITY_VERIFICATION,
            "verify",
            "Review identity verification",
        ),
        _p(C.VIEW_ROLES, M.ROLE_MANAGEMENT, "view", "View roles and appointments"),
        _p(C.MANAGE_ROLES, M.ROLE_MANAGEMENT, "manage", "Create, edit and archive roles"),
        _p(C.DELEGATE_AUTHORITY, M.ROLE_MANAGEMENT, "delegate", "Delegate authority"),
        _p(
            C.APPOINT_NATIONAL_ADMINISTRATOR,
            M.ROLE_MANAGEMENT,
            "assign",
            "Appoint National Administrators",
            protected=True,
        ),
        _p(
            C.APPROVE_ADMINISTRATOR_REMOVAL,
            M.ROLE_MANAGEMENT,
            "approve",
            "Approve suspending or removing a National Administrator",
            protected=True,
        ),
        _p(C.ASSIGN_ROLES, M.ASSIGNMENTS, "assign", "Appoint members to roles"),
        _p(
            C.MANAGE_ASSIGNMENTS,
            M.ASSIGNMENTS,
            "suspend",
            "Suspend, reactivate and revoke appointments",
        ),
        _p(C.ACCREDIT_AGENTS, M.ASSIGNMENTS, "approve", "Accredit field agents"),
        _p(C.MANAGE_AGENT_ASSIGNMENTS, M.ASSIGNMENTS, "manage", "Manage agent assignments"),
        _p(
            C.CONFIRM_AGENT_LOCATION,
            M.POLLING_UNIT_MAPPING,
            "verify",
            "Pin polling units and confirm agent locations",
        ),
        _p(C.SUBMIT_FIELD_REPORT, M.FIELD_MONITORING, "create", "Submit field reports"),
        _p(
            C.FIELD_DUTY,
            M.FIELD_MONITORING,
            "create",
            "Check in at their polling unit and share location on duty",
        ),
        _p(C.VERIFY_FIELD_REPORT, M.FIELD_MONITORING, "verify", "Verify field reports"),
        _p(C.VIEW_INCIDENTS, M.INCIDENTS, "view", "View incidents"),
        _p(C.CREATE_INCIDENT, M.INCIDENTS, "create", "Report incidents"),
        _p(C.ACKNOWLEDGE_INCIDENT, M.INCIDENTS, "verify", "Acknowledge incidents"),
        _p(C.ASSIGN_INCIDENT, M.INCIDENTS, "assign", "Assign incidents"),
        _p(C.CLOSE_INCIDENT, M.INCIDENTS, "manage", "Close incidents"),
        _p(
            C.RESPOND_TO_SECURITY,
            M.INCIDENTS,
            "respond",
            "Respond as a security officer to incidents the Situation Room sends",
        ),
        _p(C.VIEW_EVIDENCE, M.EVIDENCE, "view", "View evidence"),
        _p(C.MANAGE_EVIDENCE, M.EVIDENCE, "manage", "Manage evidence"),
        _p(C.SUBMIT_ELECTION_RESULT, M.RESULT_SUBMISSION, "create", "Submit results"),
        _p(C.VERIFY_ELECTION_RESULT, M.RESULT_VERIFICATION, "verify", "Verify results"),
        _p(C.DISPUTE_ELECTION_RESULT, M.RESULT_VERIFICATION, "reject", "Dispute results"),
        _p(C.VIEW_COLLATION, M.COLLATION, "view", "View collation"),
        _p(C.MANAGE_COLLATION, M.COLLATION, "manage", "Manage collation"),
        _p(C.VIEW_COMMUNICATIONS, M.COMMUNICATIONS, "view", "View communications"),
        _p(C.SEND_OPERATIONAL_MESSAGE, M.COMMUNICATIONS, "create", "Send operational messages"),
        _p(C.SEND_BROADCAST, M.COMMUNICATIONS, "publish", "Publish announcements and broadcasts"),
        _p(C.VIEW_DISCUSSION_ROOM, M.DISCUSSION_ROOMS, "view", "View discussion rooms"),
        _p(C.CREATE_DISCUSSION_THREAD, M.DISCUSSION_ROOMS, "create", "Start discussions"),
        _p(C.POST_DISCUSSION_REPLY, M.DISCUSSION_ROOMS, "create", "Reply in discussions"),
        _p(
            C.MODERATE_DISCUSSION,
            M.DISCUSSION_ROOMS,
            "approve",
            "Approve and hide discussion posts",
        ),
        _p(C.VIEW_MEETING_ROOM, M.MEETINGS, "view", "View meetings"),
        _p(C.START_MEETING, M.MEETINGS, "create", "Start meetings"),
        _p(C.JOIN_MEETING, M.MEETINGS, "view", "Join meetings"),
        _p(C.VIEW_MEDIA_INTELLIGENCE, M.MEDIA_INTELLIGENCE, "view", "View media intelligence"),
        _p(C.EXPORT_REPORTS, M.REPORTS, "export", "Export reports"),
        _p(
            C.VIEW_WALLETS,
            M.WALLETS,
            "view",
            "View members' wallets and payments",
            sensitive=True,
        ),
        _p(C.CREDIT_WALLETS, M.WALLETS, "create", "Request payments to members' wallets"),
        _p(C.APPROVE_WALLET_CREDITS, M.WALLETS, "approve", "Approve payments to members' wallets"),
        _p(
            C.APPROVE_WITHDRAWALS,
            M.WALLETS,
            "approve",
            "Approve withdrawals to members' bank accounts",
        ),
        _p(
            C.MANAGE_CREDIT_RATES,
            M.WALLETS,
            "manage",
            "Set what a registration earns and what a TGC token is worth",
            protected=True,
        ),
        _p(
            C.MANAGE_PROJECTS,
            M.PROJECTS,
            "manage",
            "Create and schedule projects",
            protected=True,
        ),
        _p(C.ASSIGN_PROJECT_PARTICIPANTS, M.PROJECTS, "assign", "Assign people to projects"),
        _p(C.SEND_SMS, M.COMMUNICATIONS, "publish", "Send SMS to members"),
        _p(C.SEND_WHATSAPP, M.COMMUNICATIONS, "publish", "Send WhatsApp messages to members"),
        _p(
            C.SEND_PUSH_NOTIFICATIONS,
            M.COMMUNICATIONS,
            "publish",
            "Send app notifications to members",
        ),
        _p(
            C.REVIEW_ASK_ME,
            M.ASK_ME,
            "review",
            "Draft Ask Me's library and review its answers",
        ),
        _p(
            C.APPROVE_ASK_ME,
            M.ASK_ME,
            "approve",
            "Approve what Ask Me may say, and its settings",
            protected=True,
        ),
        _p(C.VIEW_AUDIT, M.GOVERNANCE, "view", "View the audit log"),
        _p(C.MANAGE_USERS, M.SYSTEM_MONITORING, "manage", "Support user accounts"),
        _p(C.MANAGE_SYSTEM_SETTINGS, M.SYSTEM_MONITORING, "manage", "Manage system settings"),
    )
}

PROTECTED = frozenset(code for code, info in PERMISSIONS.items() if info.protected)

# Every Capability must be described, and nothing else may be.
assert set(PERMISSIONS) == set(Capability.values), "Registry and Capability are out of sync."


def is_registered(code: str) -> bool:
    return code in PERMISSIONS


def unknown_codes(codes) -> list[str]:
    return sorted(set(codes) - PERMISSIONS.keys())
