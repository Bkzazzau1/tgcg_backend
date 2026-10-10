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
