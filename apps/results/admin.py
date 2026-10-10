from django.contrib import admin

from .models import Election, Party, PartyVote, ResultEvent, ResultSubmission


@admin.register(Party)
class PartyAdmin(admin.ModelAdmin):
    list_display = ["acronym", "name", "is_active"]
    list_filter = ["is_active"]
    search_fields = ["acronym", "name"]


@admin.register(Election)
class ElectionAdmin(admin.ModelAdmin):
    list_display = ["code", "name", "type", "election_date", "status", "scope_level", "state"]
    list_filter = ["type", "status"]
    search_fields = ["code", "name"]
    filter_horizontal = ["parties"]
    autocomplete_fields = ["state", "senatorial_district", "lga", "ward", "polling_unit"]
    prepopulated_fields = {"code": ["name"]}


class PartyVoteInline(admin.TabularInline):
    model = PartyVote
    extra = 0
    can_delete = False
    readonly_fields = ["party", "votes"]

    def has_add_permission(self, request, obj=None):
        return False


class ResultEventInline(admin.TabularInline):
    model = ResultEvent
    extra = 0
    can_delete = False
    readonly_fields = ["from_status", "to_status", "note", "actor", "created_at"]

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(ResultSubmission)
class ResultSubmissionAdmin(admin.ModelAdmin):
    """Read-only: results are submitted, verified and disputed through the API."""

    list_display = [
        "reference",
        "election",
        "polling_unit",
        "status",
        "total_valid_votes",
        "arithmetic_valid",
        "duplicate_suspected",
        "submitted_at",
    ]
    list_filter = ["election", "status", "source", "arithmetic_valid", "duplicate_suspected"]
    search_fields = ["reference", "polling_unit__code"]
    inlines = [PartyVoteInline, ResultEventInline]

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in ResultSubmission._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
