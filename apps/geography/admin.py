from django.contrib import admin

from .models import Lga, PollingUnit, SenatorialDistrict, State, Ward, Zone


@admin.register(Zone)
class ZoneAdmin(admin.ModelAdmin):
    list_display = ["code", "name"]


@admin.register(State)
class StateAdmin(admin.ModelAdmin):
    list_display = ["code", "name", "zone", "is_fct"]
    list_filter = ["zone"]
    search_fields = ["name", "code"]


@admin.register(SenatorialDistrict)
class SenatorialDistrictAdmin(admin.ModelAdmin):
    list_display = ["code", "name", "state"]
    list_filter = ["state__zone", "state"]
    search_fields = ["name", "code"]


@admin.register(Lga)
class LgaAdmin(admin.ModelAdmin):
    list_display = ["code", "name", "state", "senatorial_district"]
    list_filter = ["state__zone", "state"]
    search_fields = ["name", "code"]


@admin.register(Ward)
class WardAdmin(admin.ModelAdmin):
    list_display = ["code", "name", "lga"]
    search_fields = ["name", "code"]
    autocomplete_fields = ["lga"]


@admin.register(PollingUnit)
class PollingUnitAdmin(admin.ModelAdmin):
    list_display = ["code", "name", "ward", "registered_voters"]
    search_fields = ["name", "code"]
    autocomplete_fields = ["ward"]
