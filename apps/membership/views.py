import re

from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Count, Prefetch, Q
from django.http import HttpResponse
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.access import audit
from apps.access.models import (
    AssignmentStatus,
    AuditEvent,
    RoleAssignment,
    RoleAssignmentArea,
)
from apps.access.serializers import AuditEventSerializer
from apps.accounts.phone import normalize_ng_phone
from apps.core.caching import remembered
from apps.core.choices import GeographyLevel
from apps.core.permissions import Capability, requires
from apps.geography.scope import (
    LEVEL_FIELD,
    SCOPE_FIELDS,
    ScopeRef,
    scope_contains,
    scope_contains_any,
    scope_payload,
    within_scope_any_q,
    within_scope_q,
)

from . import analysis, identity, members_list_pdf, services
from .models import (
    AccreditedAgent,
    Member,
    PollingUnitChangeRequest,
    PvcUpdate,
    VerificationCheck,
    VolunteerApplication,
)
from .registration_views import _photo_response, image_response
from .serializers import (
    AccreditationEventSerializer,
    AgentSerializer,
    DecisionSerializer,
    ExcoSerializer,
    MemberSerializer,
    PollingUnitChangeSerializer,
    PvcUpdateSerializer,
    ReadinessSerializer,
    StandingChangeSerializer,
    StatusChangeSerializer,
    VerificationNotesSerializer,
    VerificationSerializer,
    VolunteerApplicationSerializer,
    flatten_scope,
)

C = Capability
MEMBERSHIP_READERS = (
    C.MANAGE_MEMBERSHIP,
    C.REGISTER_MEMBERS,
    C.ACCREDIT_AGENTS,
    C.MANAGE_AGENT_ASSIGNMENTS,
)
# Who can find members in their areas: the above, and those who verify,
# suspend or see members' personal details there.
MEMBER_READERS = (
    *MEMBERSHIP_READERS,
    C.VERIFY_MEMBER_IDENTITY,
    C.SUSPEND_MEMBERS,
    C.VIEW_MEMBER_SENSITIVE_DATA,
)
STANDING_ACTIONS = ("suspend", "deactivate", "restore")
# The record of standing and identity decisions: for those who make them.
HISTORY_READERS = (C.VERIFY_MEMBER_IDENTITY, C.SUSPEND_MEMBERS)
VERIFICATION_ACTIONS = ("verification", "run_checks", "verify", "reject_identity")
# How deep each level lies below Nigeria (the country is 0).
DEPTH = {level: depth for depth, level in enumerate(LEVEL_FIELD, start=1)}

SCOPE_FILTERS = {
    "scope_level": ["exact"],
    "zone__code": ["exact"],
    "state__code": ["exact"],
    "senatorial_district__code": ["exact"],
    "lga__code": ["exact"],
    "ward__code": ["exact"],
    "polling_unit__code": ["exact"],
}


# A search made only of digits, spaces and phone punctuation is a phone number.
PHONE_LIKE = re.compile(r"[\d\s+()-]{10,}")
# Narrowing a list of members (`show=`); built fresh for every request.
_SHOW = analysis.MEMBER_FILTER_NAMES


def _shown(show) -> Q:
    """The filter for `show=`, or nothing for an unknown value."""
    return analysis.member_filters().get(show, Q(pk__in=[]))


AREA_PARAMETERS = [
    OpenApiParameter(
        "level", str, enum=GeographyLevel.values, description="The area; defaults to Nigeria"
    ),
    OpenApiParameter("code", str, description="The area's code"),
]


class MemberViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """Members registered within the caller's scope.

    New members are created through registration: `POST /register/` (self,
    in the app) or `POST /members/register/` (a coordinator, in person).
    """

    serializer_class = MemberSerializer
    permission_classes = [requires(C.MANAGE_MEMBERSHIP, safe=MEMBER_READERS)]
    lookup_field = "membership_number"
    http_method_names = ["get", "post", "patch", "head", "options"]
    filterset_fields = {
        "status": ["exact"],
        "verification_status": ["exact"],
        "membership_state": ["exact"],
        "origin": ["exact"],
        "registration_channel": ["exact"],
        "gender": ["exact"],
        **SCOPE_FILTERS,
    }
    # Phone numbers are personal: find by phone with ?phone=, inside the
    # areas where the caller may see personal details.
    search_fields = ["full_name", "membership_number"]
    ordering_fields = ["created_at", "full_name"]

    def get_permissions(self):
        if self.action == "photo":
            return [requires(C.VIEW_MEMBER_SENSITIVE_DATA)()]
        if self.action in STANDING_ACTIONS:
            return [requires(C.SUSPEND_MEMBERS)()]
        if self.action in VERIFICATION_ACTIONS:
            return [requires(C.VERIFY_MEMBER_IDENTITY)()]
        if self.action == "history":
            return [requires(*HISTORY_READERS)()]
        return super().get_permissions()

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):  # schema generation
            return Member.objects.none()
        user = self.request.user
        members = (
            Member.objects.filter(within_scope_any_q(user, MEMBER_READERS))
            .select_related("user", *SCOPE_FIELDS)
            .prefetch_related(
                Prefetch("accreditations"),
                Prefetch(
                    "role_assignments",
                    queryset=RoleAssignment.objects.select_related("role")
                    .filter(status__in=[AssignmentStatus.ACTIVE, AssignmentStatus.SUSPENDED])
                    .prefetch_related(
                        Prefetch(
                            "areas",
                            queryset=RoleAssignmentArea.objects.select_related(*SCOPE_FIELDS),
                        )
                    ),
                ),
            )
        )
        params = self.request.query_params
        if self.action == "list":
            if show := params.get("show"):
                members = members.filter(_shown(show))
            if role := params.get("role"):
                holders = analysis.exco_areas().filter(assignment__role__code=role)
                members = members.filter(pk__in=holders.values("assignment__member_id"))
            if text := params.get("q", "").strip():
                members = members.filter(self._matching(user, text))
        phone = self.request.query_params.get("phone")
        if phone and self.action == "list":
            members = members.filter(
                within_scope_q(user, permission=C.VIEW_MEMBER_SENSITIVE_DATA),
                phone_number=normalize_ng_phone(phone) or phone,
            )
        return members

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "phone",
                str,
                description="Exact phone number; matches only where you may see personal details.",
            ),
            OpenApiParameter(
                "show",
                str,
                enum=[*_SHOW],
                description="Filter by profile gaps, verification source, recorded sign-in, "
                "app account or exco appointment.",
            ),
            OpenApiParameter(
                "q",
                str,
                description="Words to find in names, membership numbers, places and current "
                "posts, or a phone number",
            ),
            OpenApiParameter("role", str, description="Only those holding this post now"),
        ]
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @staticmethod
    def _matching(user, text):
        """Search: a phone number finds its member where the caller may see
        personal details; words find names, numbers, places and posts."""
        if PHONE_LIKE.fullmatch(text) and (phone := normalize_ng_phone(text)):
            return within_scope_q(user, permission=C.VIEW_MEMBER_SENSITIVE_DATA) & Q(
                phone_number=phone
            )
        return analysis.search_q(text)

    def _area(self, request):
        """The area asked for, which must lie wholly within the caller's reach."""
        level = request.query_params.get("level") or GeographyLevel.COUNTRY
        scope = ScopeRef.resolve(level, request.query_params.get("code"))
        if not scope_contains_any(request.user, scope, MEMBER_READERS):
            raise PermissionDenied("This area is outside your access.")
        return scope

    @extend_schema(
        parameters=[
            *AREA_PARAMETERS,
            OpenApiParameter(
                "by",
                str,
                description="The smaller areas to list: one of `breakdowns` (Nigeria by state "
                "or by geopoliticalZone); the first by default",
            ),
        ],
        responses={200: OpenApiTypes.OBJECT},
    )
    @action(detail=False, methods=["get"])
    def analysis(self, request):
        """Members and exco in an area, wholly within the caller's reach.

        `members`: totals, new in 7 and 30 days, fully verified, by
        verification, standing, gender, age, how they joined, what their
        profiles hold, and new members week by week. `exco`: people and posts,
        how many are here (at this area itself) or suspended, by level and
        role, and their profiles. `parts`: each smaller area (`by`) with its
        members and exco.
        """
        scope = self._area(request)
        by = request.query_params.get("by") or None
        if by is not None and by not in analysis.breakdowns(scope):
            raise ValidationError({"by": "This area cannot be broken down that way."})
        return Response(
            remembered(
                "member-analysis",
                {"scope": scope_payload(scope), "by": by},
                lambda: {
                    "scope": scope_payload(scope),
                    "breakdowns": analysis.breakdowns(scope),
                    **analysis.summary(scope, by),
                },
            )
        )

    @extend_schema(
        parameters=[
            *AREA_PARAMETERS,
            OpenApiParameter(
                "at",
                str,
                description="Only posts at these levels, comma-separated, e.g. state,lga",
            ),
            OpenApiParameter("role", str, description="A role's code"),
            OpenApiParameter("show", str, enum=[*_SHOW]),
            OpenApiParameter("search", str, description="Name, membership number or role"),
        ],
        responses=ExcoSerializer(many=True),
    )
    @action(detail=False, methods=["get"])
    def exco(self, request):
        """Exco posts in an area, from the top down (state, district, LGA,
        ward, polling unit), with whoever holds each and how complete their
        profile is."""
        scope = self._area(request)
        params = request.query_params
        posts = analysis.exco_areas(scope).select_related(
            "assignment__role",
            "assignment__member__user",
            *(f"assignment__member__{field}" for field in SCOPE_FIELDS),
            *SCOPE_FIELDS,
        )
        if at := [level for level in params.get("at", "").split(",") if level]:
            posts = posts.filter(scope_level__in=at)
        if role := params.get("role"):
            posts = posts.filter(assignment__role__code=role)
        if show := params.get("show"):
            posts = posts.filter(assignment__member__in=Member.objects.filter(_shown(show)))
        if search := params.get("search", "").strip():
            posts = posts.filter(
                Q(assignment__member__full_name__icontains=search)
                | Q(assignment__member__membership_number__iexact=search)
                | Q(assignment__role__name__icontains=search)
            )
        posts = posts.annotate(depth=analysis.depth_order()).order_by(
            "depth",
            "zone__name",
            "state__name",
            "senatorial_district__name",
            "lga__name",
            "ward__name",
            "polling_unit__name",
            "assignment__role__name",
            "assignment__member__full_name",
            "pk",
        )
        page = self.paginate_queryset(posts)
        context = self.get_serializer_context()
        return self.get_paginated_response(ExcoSerializer(page, many=True, context=context).data)

    @extend_schema(parameters=AREA_PARAMETERS, responses={(200, "application/pdf"): bytes})
    @action(detail=False, methods=["get"], url_path="list-pdf")
    def list_pdf(self, request):
        """Members and exco in an area as one printable list (PDF), for an area
        wholly within the caller's reach: grouped by place, exco first, each
        row with the full name, membership number, polling unit and posts. No
        phone numbers. Each download is recorded in the audit log."""
        scope = self._area(request)
        listing = members_list_pdf.collect(scope)
        made_at = timezone.localtime()
        pdf = members_list_pdf.render(listing, made_by=request.user.access_id, made_at=made_at)
        field = scope.scope_unit_field
        audit.record(
            actor=request.user,
            action="members.list_downloaded",
            target_type="area",
            target_id=getattr(scope, field).code if field else "NG",
            target_label=listing.title,
            scope=[scope_payload(scope)],
            changes={"people": listing.people, "exco": listing.exco},
        )
        response = HttpResponse(pdf, content_type="application/pdf")
        name = members_list_pdf.filename(listing, made_at)
        response["Content-Disposition"] = f'attachment; filename="{name}"'
        response["Cache-Control"] = "no-store"
        return response

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "level",
                str,
                enum=GeographyLevel.values,
                description="The area; defaults to Nigeria",
            ),
            OpenApiParameter("code", str, description="The area's code"),
            OpenApiParameter(
                "by",
                str,
                enum=[*LEVEL_FIELD],
                required=True,
                description="The smaller areas to count, e.g. ward",
            ),
        ],
        responses={200: OpenApiTypes.OBJECT},
    )
    @action(detail=False, methods=["get"])
    def counts(self, request):
        """How many members are registered in each smaller area of an area.

        Only an area wholly within the caller's reach is counted, so no count is
        partial. Members registered above the `by` level (for example only at
        their LGA) are in `total` but in no part.
        """
        level = request.query_params.get("level") or GeographyLevel.COUNTRY
        scope = ScopeRef.resolve(level, request.query_params.get("code"))
        if not scope_contains_any(request.user, scope, MEMBER_READERS):
            raise PermissionDenied("This area is outside your access.")
        by = request.query_params.get("by")
        if DEPTH.get(by, 0) <= DEPTH.get(scope.scope_level, 0):
            raise ValidationError({"by": "Choose a level smaller than the area."})
        members = Member.objects.filter(within_scope_q(scope)).filter(
            within_scope_any_q(request.user, MEMBER_READERS)
        )
        field = LEVEL_FIELD[by]
        parts = (
            members.exclude(**{f"{field}__isnull": True})
            .values_list(f"{field}__code")
            .annotate(n=Count("pk"))
            .order_by()
        )
        return Response(
            {
                "scope": scope_payload(scope),
                "by": by,
                "total": members.count(),
                "counts": dict(parts),
            }
        )

    @extend_schema(responses={(200, "image/jpeg"): bytes})
    @action(detail=True, methods=["get"])
    def photo(self, request, membership_number=None):
        member = self.get_object()
        if not scope_contains(request.user, member, permission=C.VIEW_MEMBER_SENSITIVE_DATA):
            raise PermissionDenied("Personal details of this member are outside your access.")
        return _photo_response(member)

    def _standing(self, request, change):
        data = StandingChangeSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        member = change(
            actor=request.user,
            member=self.get_object(),
            reason=data.validated_data.get("reason", ""),
        )
        return Response(self.get_serializer(self.get_queryset().get(pk=member.pk)).data)

    @extend_schema(request=StandingChangeSerializer, responses=MemberSerializer)
    @action(detail=True, methods=["post"])
    def suspend(self, request, membership_number=None):
        """Block all of the member's operational access (reason required)."""
        return self._standing(request, services.suspend_membership)

    @extend_schema(request=StandingChangeSerializer, responses=MemberSerializer)
    @action(detail=True, methods=["post"])
    def deactivate(self, request, membership_number=None):
        """End the membership; the member can no longer sign in (reason required)."""
        return self._standing(request, services.deactivate_membership)

    @extend_schema(request=StandingChangeSerializer, responses=MemberSerializer)
    @action(detail=True, methods=["post"])
    def restore(self, request, membership_number=None):
        return self._standing(request, services.restore_membership)

    def _verification(self, member):
        checks = VerificationCheck.objects.select_related("checked_by")
        member = (
            Member.objects.select_related("verified_by")
            .prefetch_related(Prefetch("verification_checks", queryset=checks))
            .get(pk=member.pk)
        )
        return Response(VerificationSerializer(member).data)

    def _require_reviewer(self, member):
        identity.require_reviewer(self.request.user, member)

    @extend_schema(responses=VerificationSerializer)
    @action(detail=True, methods=["get"])
    def verification(self, request, membership_number=None):
        """The identity decision and every check behind it."""
        member = self.get_object()
        self._require_reviewer(member)
        return self._verification(member)

    @extend_schema(request=None, responses=VerificationSerializer)
    @action(detail=True, methods=["post"], url_path="verification/check")
    def run_checks(self, request, membership_number=None):
        """Run the checks the system can perform again (e.g. after a PVC was added)."""
        member = self.get_object()
        self._require_reviewer(member)
        return self._verification(identity.record_checks(member, actor=request.user))

    @extend_schema(request=VerificationNotesSerializer, responses=VerificationSerializer)
    @action(detail=True, methods=["post"], url_path="verification/approve")
    def verify(self, request, membership_number=None):
        """Confirm the identity after reviewing what the system could not check."""
        data = VerificationNotesSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        member = identity.approve(
            actor=request.user, member=self.get_object(), notes=data.validated_data["notes"]
        )
        return self._verification(member)

    @extend_schema(request=DecisionSerializer, responses=VerificationSerializer)
    @action(detail=True, methods=["post"], url_path="verification/reject")
    def reject_identity(self, request, membership_number=None):
        data = DecisionSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        member = identity.reject(
            actor=request.user,
            member=self.get_object(),
            reason=data.validated_data.get("reason", ""),
        )
        return self._verification(member)

    @extend_schema(responses=AuditEventSerializer(many=True))
    @action(detail=True, methods=["get"])
    def history(self, request, membership_number=None):
        """Verification, standing and polling unit changes, newest first."""
        member = self.get_object()
        if not scope_contains_any(request.user, member, HISTORY_READERS):
            raise PermissionDenied("This member's history is outside your area.")
        events = AuditEvent.objects.filter(target_type="member", target_id=member.membership_number)
        return Response(AuditEventSerializer(events, many=True).data)

    def perform_update(self, serializer):
        serializer.instance = services.update_member(
            actor=self.request.user,
            member=serializer.instance,
            **flatten_scope(serializer.validated_data),
        )


class AgentViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    """Agent accreditations assigned within the caller's scope."""

    lookup_field = "agent_id"
    serializer_class = AgentSerializer
    filterset_fields = {"status": ["exact"], "role": ["exact"], **SCOPE_FILTERS}
    search_fields = ["agent_id", "member__full_name", "member__membership_number"]
    ordering_fields = ["created_at", "status"]

    def get_permissions(self):
        if self.action in ("create", "change_status"):
            classes = [requires(C.ACCREDIT_AGENTS, safe=MEMBERSHIP_READERS)]
        elif self.action == "readiness":
            classes = [requires(C.ACCREDIT_AGENTS, C.MANAGE_AGENT_ASSIGNMENTS)]
        else:
            classes = [requires(*MEMBERSHIP_READERS)]
        return [permission() for permission in classes]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):  # schema generation
            return AccreditedAgent.objects.none()
        user = self.request.user
        return AccreditedAgent.objects.filter(
            within_scope_any_q(user, MEMBERSHIP_READERS)
        ).select_related("member", *SCOPE_FIELDS)

    def perform_create(self, serializer):
        data = flatten_scope(serializer.validated_data)
        member = data.pop("member")
        serializer.instance = services.accredit(actor=self.request.user, member=member, **data)

    @extend_schema(request=ReadinessSerializer, responses=AgentSerializer)
    @action(detail=True, methods=["patch"])
    def readiness(self, request, agent_id=None):
        agent = self.get_object()
        serializer = ReadinessSerializer(agent, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        agent = services.update_readiness(
            actor=request.user, agent=agent, **serializer.validated_data
        )
        return Response(AgentSerializer(agent, context=self.get_serializer_context()).data)

    @extend_schema(request=StatusChangeSerializer, responses=AgentSerializer)
    @action(detail=True, methods=["post"], url_path="status")
    def change_status(self, request, agent_id=None):
        agent = self.get_object()
        serializer = StatusChangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        agent = services.change_status(actor=request.user, agent=agent, **serializer.validated_data)
        return Response(AgentSerializer(agent, context=self.get_serializer_context()).data)

    @extend_schema(responses=AccreditationEventSerializer(many=True))
    @action(detail=True, methods=["get"])
    def events(self, request, agent_id=None):
        agent = self.get_object()
        events = agent.events.select_related("actor")
        return Response(AccreditationEventSerializer(events, many=True).data)


class VolunteerViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """Members' offers of party work, for their coordinators to answer.

    Accepting an offer does not make the member an agent: agents are accredited
    separately (see AgentViewSet).
    """

    serializer_class = VolunteerApplicationSerializer
    lookup_field = "uuid"
    filterset_fields = {
        "status": ["exact"],
        **{f"member__{k}": v for k, v in SCOPE_FILTERS.items()},
    }

    def get_permissions(self):
        if self.action in ("accept", "decline"):
            return [requires(C.REGISTER_MEMBERS, C.MANAGE_MEMBERSHIP)()]
        return [requires(*MEMBERSHIP_READERS)()]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return VolunteerApplication.objects.none()
        return VolunteerApplication.objects.filter(
            within_scope_any_q(self.request.user, MEMBERSHIP_READERS, prefix="member__")
        ).select_related(
            "member__zone", "member__state", "member__lga", "member__ward", "member__polling_unit"
        )

    def _decide(self, request, decide):
        serializer = DecisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        application = decide(
            actor=request.user,
            application=self.get_object(),
            reason=serializer.validated_data.get("reason", ""),
        )
        return Response(
            VolunteerApplicationSerializer(application, context={"request": request}).data
        )

    @extend_schema(request=DecisionSerializer, responses=VolunteerApplicationSerializer)
    @action(detail=True, methods=["post"])
    def accept(self, request, uuid=None):
        return self._decide(request, services.accept_volunteer)

    @extend_schema(request=DecisionSerializer, responses=VolunteerApplicationSerializer)
    @action(detail=True, methods=["post"])
    def decline(self, request, uuid=None):
        return self._decide(request, services.decline_volunteer)


class PollingUnitChangeViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    """Members' requests to move their registered polling unit, for the
    coordinators covering the unit they ask to move to.

    Approving moves the member's registered polling unit only; their
    operational appointments are not changed.
    """

    serializer_class = PollingUnitChangeSerializer
    permission_classes = [requires(C.REVIEW_POLLING_UNIT_CHANGES)]
    lookup_field = "uuid"
    filterset_fields = {"status": ["exact"], **SCOPE_FILTERS}
    search_fields = ["member__full_name", "member__membership_number"]
    ordering_fields = ["created_at"]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return PollingUnitChangeRequest.objects.none()
        return PollingUnitChangeRequest.objects.filter(
            within_scope_q(self.request.user, permission=C.REVIEW_POLLING_UNIT_CHANGES)
        ).select_related("member", "polling_unit", "from_polling_unit", "decided_by", *SCOPE_FIELDS)

    def _decide(self, request, decide):
        data = DecisionSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        change = decide(
            actor=request.user,
            change=self.get_object(),
            reason=data.validated_data.get("reason", ""),
        )
        return Response(PollingUnitChangeSerializer(self.get_queryset().get(pk=change.pk)).data)

    @extend_schema(request=DecisionSerializer, responses=PollingUnitChangeSerializer)
    @action(detail=True, methods=["post"])
    def approve(self, request, uuid=None):
        """The updated voter card was checked: move the member's polling unit."""
        return self._decide(request, services.approve_polling_unit_change)

    @extend_schema(request=DecisionSerializer, responses=PollingUnitChangeSerializer)
    @action(detail=True, methods=["post"])
    def reject(self, request, uuid=None):
        return self._decide(request, services.reject_polling_unit_change)

    @extend_schema(responses={(200, "image/jpeg"): bytes})
    @action(detail=True, methods=["get"])
    def card(self, request, uuid=None):
        """The photo of the updated voter card, the evidence for this decision."""
        return image_response(self.get_object().pvc_image)


class PvcUpdateViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Members' PVC scans waiting for a second check, for the coordinators who
    may check identities in the member's area.

    Only members whose identity is already checked need one: other scans apply
    at once and are not listed. Approving replaces the member's details with
    the card's; the identity stays checked.
    """

    serializer_class = PvcUpdateSerializer
    permission_classes = [requires(C.VERIFY_MEMBER_IDENTITY)]
    lookup_field = "uuid"
    filterset_fields = {"status": ["exact"]}
    search_fields = ["member__full_name", "member__membership_number"]
    ordering_fields = ["created_at"]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return PvcUpdate.objects.none()
        return (
            PvcUpdate.objects.filter(
                within_scope_q(
                    self.request.user, prefix="member__", permission=C.VERIFY_MEMBER_IDENTITY
                )
            )
            .exclude(status=PvcUpdate.Status.APPLIED)
            .select_related("member__polling_unit", "polling_unit", "decided_by")
        )

    def _decide(self, request, decide):
        data = DecisionSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        update = decide(
            actor=request.user,
            update=self.get_object(),
            reason=data.validated_data.get("reason", ""),
        )
        return Response(
            PvcUpdateSerializer(
                self.get_queryset().get(pk=update.pk), context={"request": request}
            ).data
        )

    @extend_schema(request=DecisionSerializer, responses=PvcUpdateSerializer)
    @action(detail=True, methods=["post"])
    def approve(self, request, uuid=None):
        """The card shows these details and belongs to the member: they replace theirs."""
        return self._decide(request, services.approve_pvc_update)

    @extend_schema(request=DecisionSerializer, responses=PvcUpdateSerializer)
    @action(detail=True, methods=["post"])
    def reject(self, request, uuid=None):
        return self._decide(request, services.reject_pvc_update)

    @extend_schema(responses={(200, "image/jpeg"): bytes})
    @action(detail=True, methods=["get"])
    def card(self, request, uuid=None):
        """The photo of the scanned voter card, the evidence for this decision."""
        return image_response(self.get_object().pvc_image)
