from django.contrib import admin
from django.db import transaction
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView

from apps.access import views as access
from apps.accounts.views import LoginView, LogoutView, MeView, PasswordChangeView
from apps.announcements.views import AnnouncementViewSet
from apps.askme import views as askme
from apps.core.views import HealthView
from apps.field.views import (
    EvidenceDownloadView,
    FieldReportViewSet,
    IncidentViewSet,
    LiveKitWebhookView,
)
from apps.forum import views as forum
from apps.geography import views as geography
from apps.meetings.views import MeetingViewSet
from apps.membership import registration_views as reg
from apps.membership.ussd_views import UssdCallbackView
from apps.membership.views import (
    AgentViewSet,
    MemberViewSet,
    PollingUnitChangeViewSet,
    PvcUpdateViewSet,
    VolunteerViewSet,
)
from apps.messaging import views as messaging
from apps.projects import views as projects
from apps.results.views import CollationView, ElectionViewSet, PartyViewSet, ResultViewSet
from apps.security import views as security
from apps.tracking import mapping_views as mapping
from apps.tracking.views import (
    CheckInViewSet,
    CoverageView,
    DutyView,
    LiveAgentsView,
    PingsView,
)
from apps.wallets import views as wallets

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
router.register("polling-unit-changes", PollingUnitChangeViewSet, basename="polling-unit-change")
router.register("pvc-updates", PvcUpdateViewSet, basename="pvc-update")
router.register("announcements", AnnouncementViewSet, basename="announcement")
router.register("tracking/check-ins", CheckInViewSet, basename="check-in")
router.register("incidents", IncidentViewSet, basename="incident")
router.register("field-reports", FieldReportViewSet, basename="field-report")
router.register("parties", PartyViewSet)
router.register("elections", ElectionViewSet)
router.register("results", ResultViewSet, basename="result")
router.register("meetings", MeetingViewSet, basename="meeting")
router.register("forum/posts", forum.PostViewSet, basename="forum-post")
router.register("forum/replies", forum.ReplyViewSet, basename="forum-reply")
router.register("forum/reports", forum.ReportViewSet, basename="forum-report")
router.register("wallet-payments", wallets.PaymentViewSet, basename="wallet-payment")
router.register("withdrawals", wallets.WithdrawalViewSet, basename="withdrawal")
router.register("askme/questions", askme.MyQuestionViewSet, basename="askme-question")
router.register("askme/sites", askme.TrustedSiteViewSet, basename="askme-site")
router.register("askme/pages", askme.SourcePageViewSet, basename="askme-page")
router.register("askme/items", askme.KnowledgeItemViewSet, basename="askme-item")
router.register("askme/review", askme.ReviewViewSet, basename="askme-review")
router.register("me/wallet/withdrawals", wallets.MyWithdrawalViewSet, basename="my-withdrawal")
router.register("projects", projects.ProjectViewSet, basename="project")
router.register("broadcasts", messaging.BroadcastViewSet, basename="broadcast")
router.register("message-lists", messaging.MessageListViewSet, basename="message-list")
router.register(
    "project-participants", projects.ParticipationViewSet, basename="project-participant"
)
router.register("access/roles", access.RoleViewSet, basename="role")
router.register("access/assignments", access.AssignmentViewSet, basename="role-assignment")
router.register("access/delegations", access.DelegationViewSet, basename="delegation")
router.register("access/admin-actions", access.AdminActionViewSet, basename="admin-action")
router.register("access/administrators", access.AdministratorViewSet, basename="administrator")
router.register("access/audit", access.AuditEventViewSet, basename="audit-event")
router.register("me/assignments", access.MyAssignmentsViewSet, basename="my-assignment")

api_v1 = [
    path("health/", HealthView.as_view(), name="health"),
    # Not in the request transaction: failed attempts must persist for lockout.
    path("auth/login/", transaction.non_atomic_requests(LoginView.as_view()), name="auth-login"),
    path("auth/refresh/", TokenRefreshView.as_view(), name="auth-refresh"),
    path("auth/logout/", LogoutView.as_view(), name="auth-logout"),
    path("auth/password/", PasswordChangeView.as_view(), name="auth-password"),
    path("auth/me/", MeView.as_view(), name="auth-me"),
    path(
        "evidence/<uuid:uuid>/download/",
        EvidenceDownloadView.as_view(),
        name="evidence-download",
    ),
    path("collation/", CollationView.as_view(), name="collation"),
    path("livekit/webhook/", LiveKitWebhookView.as_view(), name="livekit-webhook"),
    path(
        "forum/attachments/<uuid:uuid>/",
        forum.AttachmentView.as_view(),
        name="forum-attachment",
    ),
    path("forum/settings/", forum.ForumSettingsView.as_view(), name="forum-settings"),
    path("forum/me/", forum.ForumMeView.as_view(), name="forum-me"),
    path("askme/ask/", askme.AskView.as_view(), name="askme-ask"),
    path("askme/me/", askme.MeView.as_view(), name="askme-me"),
    path("askme/settings/", askme.AskMeSettingsView.as_view(), name="askme-settings"),
    path("forum/assignments/", forum.AssignmentTagsView.as_view(), name="forum-assignments"),
    path("wallets/banks/", wallets.BanksView.as_view(), name="wallet-banks"),
    path("wallets/credit-rates/", wallets.CreditRatesView.as_view(), name="credit-rates"),
    path("paystack/webhook/", wallets.PaystackWebhookView.as_view(), name="paystack-webhook"),
    # Before wallets/<membership_number>/, which would take "registrars".
    path(
        "wallets/registrars/",
        wallets.RegistrarViewSet.as_view({"get": "list"}),
        name="registrar-list",
    ),
    path(
        "wallets/registrars/summary/",
        wallets.RegistrarViewSet.as_view({"get": "summary"}),
        name="registrar-summary",
    ),
    path(
        "wallets/registrars/<str:access_id>/registered/",
        wallets.RegistrarViewSet.as_view({"get": "registered"}),
        name="registrar-registered",
    ),
    path(
        "wallets/<str:membership_number>/",
        wallets.MemberWalletView.as_view(),
        name="member-wallet",
    ),
    path("tracking/duty/", DutyView.as_view(), name="tracking-duty"),
    path("tracking/pings/", PingsView.as_view(), name="tracking-pings"),
    path("tracking/live/", LiveAgentsView.as_view(), name="tracking-live"),
    path("tracking/coverage/", CoverageView.as_view(), name="tracking-coverage"),
    # Polling unit mapping exercise
    path("mapping/units/", mapping.MappingUnitsView.as_view(), name="mapping-units"),
    path("mapping/units/<str:code>/pin/", mapping.PinUnitView.as_view(), name="mapping-pin"),
    path(
        "mapping/units/<str:code>/problem/", mapping.ProblemView.as_view(), name="mapping-problem"
    ),
    path(
        "mapping/units/<str:code>/pins/", mapping.UnitPinsView.as_view(), name="mapping-unit-pins"
    ),
    path(
        "mapping/pins/<uuid:uuid>/accept/", mapping.AcceptPinView.as_view(), name="mapping-accept"
    ),
    path(
        "mapping/pins/<uuid:uuid>/photo/", mapping.PinPhotoView.as_view(), name="mapping-pin-photo"
    ),
    path("mapping/progress/", mapping.MappingProgressView.as_view(), name="mapping-progress"),
    # Registration. Views that check SMS codes must not run in the request
    # transaction, so failed code attempts are recorded.
    path("register/options/", reg.RegistrationOptionsView.as_view(), name="register-options"),
    # Finding a polling unit by name: states, then LGAs, wards and units.
    path("register/places/states/", reg.RegistrationStatesView.as_view(), name="register-states"),
    path("register/places/lgas/", reg.RegistrationLgasView.as_view(), name="register-lgas"),
    path("register/places/wards/", reg.RegistrationWardsView.as_view(), name="register-wards"),
    path(
        "register/places/polling-units/",
        reg.RegistrationPollingUnitsView.as_view(),
        name="register-polling-units",
    ),
    path("register/code/", reg.RegistrationCodeView.as_view(), name="register-code"),
    path(
        "register/",
        transaction.non_atomic_requests(reg.SelfRegistrationView.as_view()),
        name="register",
    ),
    path("register/polling-unit/", reg.DelimLookupView.as_view(), name="register-polling-unit"),
    path("register/read-pvc/", reg.ReadPvcView.as_view(), name="register-read-pvc"),
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
    # Joining by USSD: the provider's callback, behind a secret token
    path("ussd/<str:token>/", UssdCallbackView.as_view(), name="ussd-callback"),
    # Member home
    path("access/permissions/", access.PermissionRegistryView.as_view(), name="permissions"),
    path("me/access/", access.MyAccessView.as_view(), name="my-access"),
    path("me/member/", reg.MyMemberView.as_view(), name="my-member"),
    path("me/member/photo/", reg.MyMemberPhotoView.as_view(), name="my-member-photo"),
    path("me/member/card/", reg.MyMemberCardView.as_view(), name="my-member-card"),
    path("me/member/pvc-scan/", reg.MyPvcScanView.as_view(), name="my-pvc-scan"),
    path(
        "me/member/pvc-scan/<uuid:uuid>/withdraw/",
        reg.MyPvcScanWithdrawView.as_view(),
        name="my-pvc-scan-withdraw",
    ),
    # Members someone registered with a name and a phone number, and completes
    path("me/registered/", reg.RegisteredMembersView.as_view(), name="registered-members"),
    path(
        "me/registered/<str:number>/",
        reg.RegisteredMemberView.as_view(),
        name="registered-member",
    ),
    path(
        "me/registered/<str:number>/photo/",
        reg.RegisteredMemberPhotoView.as_view(),
        name="registered-member-photo",
    ),
    path(
        "me/registered/<str:number>/pvc-scan/",
        reg.RegisteredMemberPvcScanView.as_view(),
        name="registered-member-pvc-scan",
    ),
    path(
        "me/registered/<str:number>/pvc-scan/<uuid:uuid>/withdraw/",
        reg.RegisteredMemberPvcScanWithdrawView.as_view(),
        name="registered-member-pvc-scan-withdraw",
    ),
    path("me/polling-unit/", reg.MyPollingUnitView.as_view(), name="my-polling-unit"),
    path(
        "me/polling-unit/changes/",
        reg.MyPollingUnitChangeView.as_view(),
        name="my-polling-unit-changes",
    ),
    path(
        "me/polling-unit/changes/<uuid:uuid>/withdraw/",
        reg.MyPollingUnitChangeWithdrawView.as_view(),
        name="my-polling-unit-change-withdraw",
    ),
    path("me/announcements/", reg.MyAnnouncementsView.as_view(), name="my-announcements"),
    path("me/wallet/", wallets.MyWalletView.as_view(), name="my-wallet"),
    path(
        "me/wallet/bank-account/",
        wallets.MyBankAccountView.as_view(),
        name="my-wallet-bank-account",
    ),
    path("me/wallet/convert/", wallets.ConvertCreditsView.as_view(), name="my-wallet-convert"),
    path("me/projects/", projects.MyProjectsView.as_view(), name="my-projects"),
    path("me/messages/", messaging.MyMessagesView.as_view(), name="my-messages"),
    path("me/devices/", messaging.MyDevicesView.as_view(), name="my-devices"),
    path(
        "me/projects/<uuid:uuid>/<str:answer>/",
        projects.MyProjectAnswerView.as_view(),
        name="my-project-answer",
    ),
    path("me/volunteer/", reg.MyVolunteerView.as_view(), name="my-volunteer"),
    path(
        "me/volunteer/<uuid:uuid>/withdraw/",
        reg.MyVolunteerWithdrawView.as_view(),
        name="my-volunteer-withdraw",
    ),
    # Security officers: incidents the Situation Room sends them, and their answers
    path(
        "incidents/<str:reference>/security/",
        security.IncidentSecurityView.as_view(),
        name="incident-security",
    ),
    path(
        "incidents/<str:reference>/security/nearest/",
        security.NearestOfficersView.as_view(),
        name="incident-security-nearest",
    ),
    path(
        "security/dispatches/<uuid:uuid>/cancel/",
        security.CancelDispatchView.as_view(),
        name="security-dispatch-cancel",
    ),
    path("me/security/", security.MyDispatchesView.as_view(), name="my-security"),
    path(
        "me/security/<uuid:uuid>/respond/",
        security.RespondView.as_view(),
        name="my-security-respond",
    ),
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
