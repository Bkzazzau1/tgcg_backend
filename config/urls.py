from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView

from apps.accounts.views import LoginView, LogoutView, MeView
from apps.core.views import HealthView
from apps.field.views import EvidenceDownloadView, FieldReportViewSet, IncidentViewSet
from apps.geography import views as geography
from apps.membership.views import AgentViewSet, MemberViewSet
from apps.results.views import CollationView, ElectionViewSet, PartyViewSet, ResultViewSet

router = DefaultRouter()
router.register("geography/zones", geography.ZoneViewSet)
router.register("geography/states", geography.StateViewSet)
router.register("geography/senatorial-districts", geography.SenatorialDistrictViewSet)
router.register("geography/lgas", geography.LgaViewSet)
router.register("geography/wards", geography.WardViewSet)
router.register("geography/polling-units", geography.PollingUnitViewSet)
router.register("members", MemberViewSet, basename="member")
router.register("agents", AgentViewSet, basename="agent")
router.register("incidents", IncidentViewSet, basename="incident")
router.register("field-reports", FieldReportViewSet, basename="field-report")
router.register("parties", PartyViewSet)
router.register("elections", ElectionViewSet)
router.register("results", ResultViewSet, basename="result")

api_v1 = [
    path("health/", HealthView.as_view(), name="health"),
    path("auth/login/", LoginView.as_view(), name="auth-login"),
    path("auth/refresh/", TokenRefreshView.as_view(), name="auth-refresh"),
    path("auth/logout/", LogoutView.as_view(), name="auth-logout"),
    path("auth/me/", MeView.as_view(), name="auth-me"),
    path(
        "evidence/<uuid:uuid>/download/",
        EvidenceDownloadView.as_view(),
        name="evidence-download",
    ),
    path("collation/", CollationView.as_view(), name="collation"),
    path("", include(router.urls)),
]

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", include(api_v1)),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="docs"),
]

admin.site.site_header = "TGCG-EMCOP Administration"
admin.site.site_title = "TGCG-EMCOP"
