from django.contrib import admin

from .models import Admission, Meeting


class AdmissionInline(admin.TabularInline):
    model = Admission
    extra = 0
    readonly_fields = ["user", "first_admitted_at", "last_admitted_at", "passes"]
    can_delete = False


@admin.register(Meeting)
class MeetingAdmin(admin.ModelAdmin):
    list_display = ["reference", "title", "kind", "audience", "starts_at", "host"]
    list_filter = ["kind", "audience"]
    search_fields = ["reference", "title"]
    readonly_fields = ["reference", "uuid", "host", "ended_at", "ended_by", "cancelled_at"]
    filter_horizontal = ["invitees"]
    inlines = [AdmissionInline]
