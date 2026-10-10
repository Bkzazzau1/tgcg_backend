from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.forms import UserCreationForm

from .models import User


class TgcgUserCreationForm(UserCreationForm):
    class Meta:
        model = User
        fields = ["access_id", "full_name"]


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    add_form = TgcgUserCreationForm
    ordering = ["full_name"]
    list_display = ["access_id", "full_name", "role", "scope_level", "state", "is_active"]
    list_filter = ["role", "scope_level", "is_active", "zone", "state"]
    search_fields = ["access_id", "full_name", "phone_number", "email"]
    autocomplete_fields = ["state", "senatorial_district", "lga", "ward", "polling_unit"]
    fieldsets = [
        (None, {"fields": ["access_id", "password"]}),
        ("Profile", {"fields": ["full_name", "phone_number", "email", "role"]}),
        (
            "Scope",
            {
                "description": (
                    "Set the level and its unit; parent units are filled in automatically."
                ),
                "fields": [
                    "scope_level",
                    "zone",
                    "state",
                    "senatorial_district",
                    "lga",
                    "ward",
                    "polling_unit",
                ],
            },
        ),
        (
            "Permissions",
            {"fields": ["is_active", "is_staff", "is_superuser", "groups", "user_permissions"]},
        ),
        ("Dates", {"fields": ["last_login", "date_joined"]}),
    ]
    add_fieldsets = [
        (
            None,
            {"classes": ["wide"], "fields": ["access_id", "full_name", "password1", "password2"]},
        ),
    ]
