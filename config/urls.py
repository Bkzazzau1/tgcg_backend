from django.contrib import admin
from django.db import transaction
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView

from apps.accounts.views import LoginView, LogoutView, MeView
from apps.announcements.views import AnnouncementViewSet
from apps.core.views import HealthView
from apps.field.views import EvidenceDownloadView, FieldReportViewSet, IncidentViewSet
from apps.geography import views as geography
from apps.membership import registration_views as reg
from apps.membership.views import AgentViewSet, MemberViewSet, VolunteerViewSet
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
router.register("volunteers", VolunteerViewSet, basename="volunteer")
router.register("announcements", AnnouncementViewSet, basename="announcement")
router.register("incidents", IncidentViewSet, basename="incident")
router.register("field-reports", FieldReportViewSet, basename="field-report")
router.register("parties", PartyViewSet)
router.register("elections", ElectionViewSet)
router.register("results", ResultViewSet, basename="result")

api_v1 = [
    path("health/", HealthView.as_view(), name="health"),
    # Not in the request transaction: failed attempts must persist for lockout.
    path("auth/login/", transaction.non_atomic_requests(LoginView.as_view()), name="auth-login"),
    path("auth/refresh/", TokenRefreshView.as_view(), name="auth-refresh"),
    path("auth/logout/", LogoutView.as_view(), name="auth-logout"),
    path("auth/me/", MeView.as_view(), name="auth-me"),
    path(
        "evidence/<uuid:uuid>/download/",
        EvidenceDownloadView.as_view(),
        name="evidence-download",
    ),
    path("collation/", CollationView.as_view(), name="collation"),
    # Registration. Views that check SMS codes must not run in the request
    # transaction, so failed code attempts are recorded.
    path("register/code/", reg.RegistrationCodeView.as_view(), name="register-code"),
    path(
        "register/",
        transaction.non_atomic_requests(reg.SelfRegistrationView.as_view()),
        name="register",
    ),
    path("register/pin-code/", reg.PinCodeView.as_view(), name="register-pin-code"),
    path(
        "register/pin/",
        transaction.non_atomic_requests(reg.SetPinView.as_view()),
        name="register-pin",
    ),
    path(
        "members/register/",
        transaction.non_atomic_requests(reg.AssistedRegistrationView.as_view()),
        name="member-register",
    ),
    path("members/verify/", reg.CardVerificationView.as_view(), name="member-card-verify"),
    path("members/verify/photo/", reg.CardPhotoView.as_view(), name="member-card-photo"),
    # Member home
    path("me/member/", reg.MyMemberView.as_view(), name="my-member"),
    path("me/member/photo/", reg.MyMemberPhotoView.as_view(), name="my-member-photo"),
    path("me/polling-unit/", reg.MyPollingUnitView.as_view(), name="my-polling-unit"),
    path("me/announcements/", reg.MyAnnouncementsView.as_view(), name="my-announcements"),
    path("me/volunteer/", reg.MyVolunteerView.as_view(), name="my-volunteer"),
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
