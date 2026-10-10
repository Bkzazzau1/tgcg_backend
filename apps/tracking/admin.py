from django.contrib import admin

from .models import AgentCheckIn, DutySession


@admin.register(AgentCheckIn)
class AgentCheckInAdmin(admin.ModelAdmin):
    list_display = ["agent", "polling_unit", "location_status", "distance_to_pu_m", "created_at"]
    list_filter = ["location_status"]
    search_fields = ["agent__access_id", "polling_unit__code"]
    readonly_fields = [f.name for f in AgentCheckIn._meta.fields]

    def has_add_permission(self, request):
        return False


@admin.register(DutySession)
class DutySessionAdmin(admin.ModelAdmin):
    list_display = ["user", "started_at", "ended_at"]
    readonly_fields = ["user", "started_at", "ended_at"]

    def has_add_permission(self, request):
        return False
