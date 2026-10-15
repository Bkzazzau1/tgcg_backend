"""Enumerations shared with the Flutter client (lib/tgcg/domain/models.dart).

Stored values use the client's camelCase enum names so payloads map 1:1
onto `Enum.values.byName(...)` in Dart.
"""

from django.db import models


class GeographyLevel(models.TextChoices):
    COUNTRY = "country", "Country"
    GEOPOLITICAL_ZONE = "geopoliticalZone", "Geopolitical zone"
    STATE = "state", "State"
    SENATORIAL_DISTRICT = "senatorialDistrict", "Senatorial district"
    LGA = "lga", "Local government area"
    WARD = "ward", "Ward"
    POLLING_UNIT = "pollingUnit", "Polling unit"


class Role(models.TextChoices):
    NATIONAL_ADMINISTRATOR = "nationalAdministrator", "National administrator"
    NATIONAL_COLLATION_OFFICER = "nationalCollationOfficer", "National collation officer"
    SITUATION_ROOM_DIRECTOR = "situationRoomDirector", "Situation room director"
    ZONAL_COORDINATOR = "zonalCoordinator", "Zonal coordinator"
    STATE_COORDINATOR = "stateCoordinator", "State coordinator"
    LGA_COORDINATOR = "lgaCoordinator", "LGA coordinator"
    WARD_COORDINATOR = "wardCoordinator", "Ward coordinator"
    POLLING_UNIT_AGENT = "pollingUnitAgent", "Polling unit agent"
    OBSERVER = "observer", "Observer"
    LEGAL_OFFICER = "legalOfficer", "Legal officer"
    TECHNICAL_SUPPORT = "technicalSupport", "Technical support"
    READ_ONLY_EXECUTIVE = "readOnlyExecutive", "Read-only executive"
    MEMBER = "member", "Member"


class RecordStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    SUBMITTED = "submitted", "Submitted"
    UNDER_REVIEW = "underReview", "Under review"
    VERIFIED = "verified", "Verified"
    DISPUTED = "disputed", "Disputed"
    REJECTED = "rejected", "Rejected"
    ARCHIVED = "archived", "Archived"


class RecordOrigin(models.TextChoices):
    LOCAL_ENTRY = "localEntry", "Local entry"
    IMPORTED = "imported", "Imported"
    SYSTEM_DERIVED = "systemDerived", "System derived"
    SMS = "sms", "SMS"
    USSD = "ussd", "USSD"
    MANUAL_OVERRIDE = "manualOverride", "Manual override"


class IncidentSeverity(models.TextChoices):
    INFO = "info", "Info"
    LOW = "low", "Low"
    MEDIUM = "medium", "Medium"
    HIGH = "high", "High"
    CRITICAL = "critical", "Critical"


class IncidentStatus(models.TextChoices):
    REPORTED = "reported", "Reported"
    ACKNOWLEDGED = "acknowledged", "Acknowledged"
    ASSIGNED = "assigned", "Assigned"
    INVESTIGATING = "investigating", "Investigating"
    ESCALATED = "escalated", "Escalated"
    RESOLVED = "resolved", "Resolved"
    CLOSED = "closed", "Closed"


class AccreditationStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    APPROVED = "approved", "Approved"
    SUSPENDED = "suspended", "Suspended"
    REVOKED = "revoked", "Revoked"


class EvidenceType(models.TextChoices):
    PHOTO = "photo", "Photo"
    VIDEO = "video", "Video"
    AUDIO = "audio", "Audio"
    DOCUMENT = "document", "Document"
    RESULT_FORM = "resultForm", "Result form"
    LOCATION = "location", "Location"


# Category values are the exact strings the Flutter client offers
# (lib/tgcg/field/field_monitoring_page.dart).
class IncidentCategory(models.TextChoices):
    ACCESS = "Access"
    SECURITY = "Security"
    VIOLENCE_THREAT = "Violence / threat"
    EVIDENCE_QUALITY = "Evidence quality"
    GEOLOCATION = "Geolocation"
    TECHNICAL = "Technical"
    MATERIALS_LOGISTICS = "Materials / logistics"
    PROCESS_IRREGULARITY = "Process irregularity"
    OTHER = "Other"


class ReportCategory(models.TextChoices):
    OPENING_STATUS = "Opening status"
    OPERATIONAL_UPDATE = "Operational update"
    MATERIALS_UPDATE = "Materials update"
    QUEUE_TURNOUT = "Queue / turnout observation"
    CONNECTIVITY_UPDATE = "Connectivity update"
    CLOSING_STATUS = "Closing status"
    AGENT_CHECK_IN = "Agent check-in"
    OTHER = "Other"


SEVERITY_RANK = {
    IncidentSeverity.INFO: 1,
    IncidentSeverity.LOW: 2,
    IncidentSeverity.MEDIUM: 3,
    IncidentSeverity.HIGH: 4,
    IncidentSeverity.CRITICAL: 5,
}


class SubmissionSource(models.TextChoices):
    APP = "app", "App"
    SMS = "sms", "SMS"
    USSD = "ussd", "USSD"
    MANUAL = "manual", "Manual"


class ElectionType(models.TextChoices):
    PRESIDENTIAL = "presidential", "Presidential"
    GOVERNORSHIP = "governorship", "Governorship"
    SENATORIAL = "senatorial", "Senatorial"
    HOUSE_OF_REPRESENTATIVES = "houseOfRepresentatives", "House of Representatives"
    STATE_ASSEMBLY = "stateAssembly", "State House of Assembly"
    AREA_COUNCIL = "areaCouncil", "LGA / area council"


class ElectionStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    OPEN = "open", "Open for results"
    CLOSED = "closed", "Closed"


class Gender(models.TextChoices):
    MALE = "male", "Male"
    FEMALE = "female", "Female"


class RegistrationChannel(models.TextChoices):
    SELF = "self", "Self-registration (app)"
    ASSISTED = "assisted", "Registered by a coordinator"
    USSD = "ussd", "Self-registration by USSD"
    IMPORT = "import", "Imported from a chapter's list"


class VerificationStatus(models.TextChoices):
    """Whether a member's identity is verified, which operational roles require."""

    UNVERIFIED = "unverified", "Not verified"
    PENDING_REVIEW = "pendingReview", "Awaiting review"
    VERIFIED = "verified", "Verified"
    REJECTED = "rejected", "Rejected"


class MembershipState(models.TextChoices):
    ACTIVE = "active", "Active"
    SUSPENDED = "suspended", "Suspended"
    DEACTIVATED = "deactivated", "Deactivated"
