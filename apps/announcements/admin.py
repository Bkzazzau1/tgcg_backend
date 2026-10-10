from django.contrib import admin

from .models import Announcement


@admin.register(Announcement)
class AnnouncementAdmin(admin.ModelAdmin):
    list_display = ["title", "scope_level", "state", "lga", "published_at", "is_published"]
    list_filter = ["is_published", "scope_level", "state"]
    search_fields = ["title", "body"]
    autocomplete_fields = ["state", "senatorial_district", "lga", "ward", "polling_unit"]
    readonly_fields = ["author"]

    def save_model(self, request, obj, form, change):
        if not obj.author_id:
            obj.author = request.user
        super().save_model(request, obj, form, change)
