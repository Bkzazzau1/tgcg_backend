from django.contrib import admin

from .models import Evidence, FieldReport, Incident, IncidentEvent


class EvidenceInline(admin.TabularInline):
    model = Evidence
    extra = 0
    can_delete = False
    fields = ["type", "file_name", "size", "content_hash", "uploader", "created_at"]
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


class IncidentEventInline(admin.TabularInline):
    model = IncidentEvent
    extra = 0
    can_delete = False
    readonly_fields = ["action", "from_status", "to_status", "note", "actor", "created_at"]

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Incident)
class IncidentAdmin(admin.ModelAdmin):
    list_display = ["reference", "title", "severity", "status", "state", "lga", "reported_at"]
    list_filter = ["status", "severity", "category", "zone", "state"]
    search_fields = ["reference", "title", "summary"]
    # Workflow changes go through the API so they are validated and audited.
    readonly_fields = [
        "reference",
        "uuid",
        "status",
        "assigned_team",
        "reporter",
        "acknowledged_at",
        "resolved_at",
        "closed_at",
        "created_at",
        "updated_at",
    ]
    autocomplete_fields = ["state", "senatorial_district", "lga", "ward", "polling_unit"]
    inlines = [EvidenceInline, IncidentEventInline]


@admin.register(FieldReport)
class FieldReportAdmin(admin.ModelAdmin):
    list_display = ["reference", "category", "status", "state", "lga", "reported_at"]
    list_filter = ["status", "category", "zone", "state"]
    search_fields = ["reference", "summary"]
    readonly_fields = [
        "reference",
        "uuid",
        "status",
        "reporter",
        "reviewed_by",
        "reviewed_at",
        "review_reason",
        "created_at",
        "updated_at",
    ]
    autocomplete_fields = ["state", "senatorial_district", "lga", "ward", "polling_unit"]
    inlines = [EvidenceInline]


@admin.register(Evidence)
class EvidenceAdmin(admin.ModelAdmin):
    list_display = ["file_name", "type", "size", "uploader", "incident", "report", "created_at"]
    list_filter = ["type"]
    search_fields = ["file_name", "content_hash", "caption"]
    readonly_fields = [f.name for f in Evidence._meta.fields]

    def has_add_permission(self, request):
        return False
