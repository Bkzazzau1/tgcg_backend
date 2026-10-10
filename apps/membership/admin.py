from django.contrib import admin

from .models import AccreditationEvent, AccreditedAgent, Member


class AccreditationEventInline(admin.TabularInline):
    model = AccreditationEvent
    extra = 0
    can_delete = False
    readonly_fields = ["from_status", "to_status", "reason", "actor", "created_at"]

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Member)
class MemberAdmin(admin.ModelAdmin):
    list_display = ["membership_number", "full_name", "phone_number", "state", "lga", "status"]
    list_filter = ["status", "origin", "zone", "state"]
    search_fields = ["membership_number", "full_name", "phone_number", "email"]
    readonly_fields = ["membership_number", "uuid", "created_by", "created_at", "updated_at"]
    autocomplete_fields = ["state", "senatorial_district", "lga", "ward", "polling_unit", "user"]


@admin.register(AccreditedAgent)
class AccreditedAgentAdmin(admin.ModelAdmin):
    list_display = ["agent_id", "member", "role", "status", "state", "lga", "polling_unit"]
    list_filter = ["status", "role", "zone", "state"]
    search_fields = ["agent_id", "member__full_name", "member__membership_number"]
    # Status changes go through the API so they are validated and audited.
    readonly_fields = [
        "agent_id",
        "uuid",
        "status",
        "status_changed_at",
        "created_by",
        "created_at",
        "updated_at",
    ]
    autocomplete_fields = ["member", "state", "senatorial_district", "lga", "ward", "polling_unit"]
    inlines = [AccreditationEventInline]
