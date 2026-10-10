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
