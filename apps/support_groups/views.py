from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.generics import ListAPIView
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny, BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.accounts.phone import local_ng_phone

from . import reading, services
from .models import GroupKind, GroupMember, SupportGroup
from .places import Places
from .serializers import (
    AddPeopleSerializer,
    AnswerSerializer,
    CodeSerializer,
    GroupKindSerializer,
    GroupMessageSerializer,
    GroupReasonSerializer,
    KindChangeSerializer,
    KindChoiceSerializer,
    LedGroupSerializer,
    MyPlaceSerializer,
    PlaceInGroupSerializer,
    RegisterGroupSerializer,
    VettedGroupSerializer,
    VettedPlaceSerializer,
)

M = GroupMember.Status
PLACES = ("member__state", "member__lga", "member__ward")


class VetsGroups(BasePermission):
    message = "Only those who vet support groups may see this."

    def has_permission(self, request, view):
        return services.vets(request.user)


def _groups():
    return SupportGroup.objects.select_related(
        "kind",
        "leader__state",
        "leader__lga",
        "leader__ward",
        "zone",
        "state",
        "registered_by",
        "decided_by",
    )


def _places(group, status=None):
    places = group.memberships.select_related("member", *PLACES).order_by("-created_at", "-id")
    if status:
        places = places.filter(status=status)
    else:
        places = places.filter(status__in=(M.ACTIVE, M.INVITED))
    return places


STATUS = OpenApiParameter("status", str, description="pending, approved, rejected or suspended")
KIND = OpenApiParameter("kind", str, description="Only groups of this type (its code)")
PLACE_STATUS = OpenApiParameter(
    "status", str, description="active or invited (both when left out), or another status"
)
TOTALS = inline_serializer(
    "SupportGroupTotals",
    {
        "groups": serializers.DictField(child=serializers.IntegerField()),
        "people": serializers.IntegerField(),
        "approved_people": serializers.IntegerField(),
        "kinds": inline_serializer(
            "SupportGroupKindTotals",
            {
                "code": serializers.CharField(),
                "name": serializers.CharField(),
                "offered": serializers.BooleanField(),
                "groups": serializers.DictField(child=serializers.IntegerField()),
                "people": serializers.IntegerField(),
            },
            many=True,
        ),
    },
)


def _kind(code) -> GroupKind:
    kind = GroupKind.objects.filter(code=(code or "").strip()).first()
    if kind is None:
        raise serializers.ValidationError({"kind": "That type of group was not found."})
    return kind


class SupportGroupViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    """Every support group, for those who vet them (manageSupportGroups for
    all of Nigeria): newest first, with how many are in each."""

    serializer_class = VettedGroupSerializer
    permission_classes = [VetsGroups]
    lookup_field = "uuid"

    def get_queryset(self):
        groups = services.with_counts(_groups())
        wanted = self.request.query_params.get("status", "")
        if wanted in SupportGroup.Status.values:
            groups = groups.filter(status=wanted)
        if kind := self.request.query_params.get("kind", "").strip():
            groups = groups.filter(kind__code=kind)
        search = self.request.query_params.get("search", "").strip()
        if search:
            groups = groups.filter(Q(name__icontains=search) | Q(serial_number__icontains=search))
        return groups

    @extend_schema(parameters=[STATUS, KIND])
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(request=KindChoiceSerializer, responses=VettedGroupSerializer)
    @action(detail=True, methods=["post"])
    def kind(self, request, uuid=None):
        """Move the group to another type."""
        data = KindChoiceSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        kind = _kind(data.validated_data["kind"])
        return self._answer(
            services.change_kind(actor=request.user, group=self.get_object(), kind=kind)
        )

    @extend_schema(
        methods=["get"], responses=GroupKindSerializer(many=True), description="Every type."
    )
    @extend_schema(
        methods=["post"],
        request=KindChangeSerializer,
        responses={201: GroupKindSerializer},
        description="Add a type, offered at once.",
    )
    @action(detail=False, methods=["get", "post"], pagination_class=None)
    def kinds(self, request):
        """The types of group: every one, offered or not; or add one."""
        if request.method == "POST":
            kind = services.add_kind(actor=request.user, name=request.data.get("name", ""))
            return Response(GroupKindSerializer(kind).data, status=status.HTTP_201_CREATED)
        return Response(GroupKindSerializer(GroupKind.objects.all(), many=True).data)

    @extend_schema(request=KindChangeSerializer, responses=GroupKindSerializer)
    @action(detail=False, methods=["patch"], url_path=r"kinds/(?P<code>[\w-]+)")
    def change_kind(self, request, code=None):
        """Rename a type, or stop or start offering it."""
        data = KindChangeSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        kind = services.update_kind(
            actor=request.user,
            kind=_kind(code),
            name=data.validated_data.get("name"),
            offered=data.validated_data.get("offered"),
        )
        return Response(GroupKindSerializer(kind).data)

    def _answer(self, group):
        group = services.with_counts(_groups()).get(pk=group.pk)
        return Response(VettedGroupSerializer(group).data)

    @extend_schema(request=None, responses=VettedGroupSerializer)
    @action(detail=True, methods=["post"])
    def approve(self, request, uuid=None):
        return self._answer(services.approve(actor=request.user, group=self.get_object()))

    @extend_schema(request=GroupReasonSerializer, responses=VettedGroupSerializer)
    @action(detail=True, methods=["post"])
    def reject(self, request, uuid=None):
        group = services.reject(
            actor=request.user, group=self.get_object(), reason=request.data.get("reason", "")
        )
        return self._answer(group)

    @extend_schema(request=GroupReasonSerializer, responses=VettedGroupSerializer)
    @action(detail=True, methods=["post"])
    def suspend(self, request, uuid=None):
        group = services.suspend(
            actor=request.user, group=self.get_object(), reason=request.data.get("reason", "")
        )
        return self._answer(group)

    @extend_schema(request=None, responses=VettedGroupSerializer)
    @action(detail=True, methods=["post"])
    def restore(self, request, uuid=None):
        return self._answer(services.restore(actor=request.user, group=self.get_object()))

    @extend_schema(parameters=[PLACE_STATUS], responses=VettedPlaceSerializer(many=True))
    @action(detail=True, methods=["get"])
    def members(self, request, uuid=None):
        places = _places(self.get_object(), request.query_params.get("status"))
        page = self.paginate_queryset(places)
        return self.get_paginated_response(VettedPlaceSerializer(page, many=True).data)

    @extend_schema(responses=TOTALS)
    @action(detail=False, methods=["get"])
    def totals(self, request):
        return Response(services.totals())


MINE = inline_serializer(
    "MySupportGroups",
    {
        "invitations": MyPlaceSerializer(many=True),
        "groups": MyPlaceSerializer(many=True),
        "leading": LedGroupSerializer(many=True),
        "vets": serializers.BooleanField(),
    },
)


class MySupportGroupsView(APIView):
    """The signed-in member's groups: those asking them to confirm, those
    they are in, and those they lead, with what TGCG decided."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses=MINE)
    def get(self, request):
        member = getattr(request.user, "member", None)
        vets = services.vets(request.user)
        if member is None:
            return Response({"invitations": [], "groups": [], "leading": [], "vets": vets})
        places = member.support_group_memberships.select_related(
            "group__leader", "group__kind"
        ).exclude(group__status=SupportGroup.Status.REJECTED, via=GroupMember.Via.LEADER)
        leading = services.with_counts(_groups().filter(leader=member))
        return Response(
            {
                "invitations": MyPlaceSerializer(places.filter(status=M.INVITED), many=True).data,
                "groups": MyPlaceSerializer(
                    places.filter(status=M.ACTIVE, group__status=SupportGroup.Status.APPROVED),
                    many=True,
                ).data,
                "leading": LedGroupSerializer(leading, many=True).data,
                "vets": vets,
            }
        )


class RegisterGroupView(APIView):
    """A signed-in member registers an organization, led by themselves or by
    someone they name. It waits for vetting."""

    permission_classes = [IsAuthenticated]

    @extend_schema(request=RegisterGroupSerializer, responses={201: LedGroupSerializer})
    def post(self, request):
        data = RegisterGroupSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        details = services.clean_details(**data.details())
        group = services.register_for(
            actor=request.user,
            details=details,
            leader_name=data.validated_data.get("leader_full_name", ""),
            leader_phone=data.validated_data.get("leader_phone_number", ""),
            consent=data.validated_data.get("consent", False),
        )
        group = services.with_counts(_groups()).get(pk=group.pk)
        return Response(LedGroupSerializer(group).data, status=status.HTTP_201_CREATED)


class JoinGroupView(APIView):
    """A member joins a group with its code."""

    permission_classes = [IsAuthenticated]

    @extend_schema(request=CodeSerializer, responses={201: MyPlaceSerializer})
    def post(self, request):
        data = CodeSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        place = services.join(user=request.user, code=data.validated_data["code"])
        return Response(MyPlaceSerializer(place).data, status=status.HTTP_201_CREATED)


class _MyPlaceView(APIView):
    permission_classes = [IsAuthenticated]

    def place(self, uuid):
        member = getattr(self.request.user, "member", None)
        return get_object_or_404(
            GroupMember.objects.select_related("group__leader", "group__kind"),
            uuid=uuid,
            member=member,
        )


class AnswerView(_MyPlaceView):
    """Say yes or no to a group that lists the member."""

    @extend_schema(request=AnswerSerializer, responses=MyPlaceSerializer)
    def post(self, request, uuid):
        data = AnswerSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        place = services.answer(user=request.user, place=self.place(uuid), **data.validated_data)
        return Response(MyPlaceSerializer(place).data)


class LeaveView(_MyPlaceView):
    @extend_schema(request=None, responses=MyPlaceSerializer)
    def post(self, request, uuid):
        place = services.leave(user=request.user, place=self.place(uuid))
        return Response(MyPlaceSerializer(place).data)


# --- the leader's group -------------------------------------------------------------


class _LedGroupView(APIView):
    """Only the group's own leader."""

    permission_classes = [IsAuthenticated]

    def group(self, uuid) -> SupportGroup:
        group = get_object_or_404(_groups(), uuid=uuid)
        if not services.leads(self.request.user, group):
            raise Http404
        return group


class LedGroupView(_LedGroupView):
    @extend_schema(responses=LedGroupSerializer)
    def get(self, request, uuid):
        group = services.with_counts(_groups()).get(pk=self.group(uuid).pk)
        return Response(LedGroupSerializer(group).data)


class LedGroupMembersView(_LedGroupView, ListAPIView):
    """Who is in the group, and who it is waiting to hear from."""

    serializer_class = PlaceInGroupSerializer

    @extend_schema(parameters=[PLACE_STATUS])
    def get(self, request, uuid):
        return self.list(request)

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return GroupMember.objects.none()
        group = self.group(self.kwargs["uuid"])
        return _places(group, self.request.query_params.get("status"))


PERSON_READ = inline_serializer(
    "PersonRead",
    {
        "line": serializers.IntegerField(),
        "full_name": serializers.CharField(),
        "phone_number": serializers.CharField(),
        "gender": serializers.CharField(),
        "place": serializers.CharField(),
        "scope": serializers.DictField(allow_null=True),
        "problem": serializers.CharField(),
    },
    many=True,
)
LIST_READ = inline_serializer(
    "ListRead",
    {
        "kind": serializers.CharField(),
        "people": PERSON_READ,
        "notes": serializers.ListField(child=serializers.CharField()),
    },
)


class ReadListView(_LedGroupView):
    """Read an uploaded list and show it back, to check before adding.
    Nothing is kept; the app then sends the people to add."""

    parser_classes = [MultiPartParser, FormParser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "group_lists"

    @extend_schema(
        request={
            "multipart/form-data": inline_serializer("GroupList", {"file": serializers.FileField()})
        },
        responses=LIST_READ,
    )
    def post(self, request, uuid):
        group = self.group(uuid)
        services.require_leader(request.user, group)
        upload = request.FILES.get("file")
        if upload is None:
            raise serializers.ValidationError({"file": "Choose the file with the list."})
        found = reading.read(upload)
        places = Places()
        people = []
        for person in found["people"]:
            scope, label = places.place(
                state=person["state"], lga=person["lga"], ward=person["ward"], unit=person["unit"]
            )
            people.append(
                {
                    "line": person["line"],
                    "full_name": person["full_name"],
                    "phone_number": local_ng_phone(person["phone_number"]),
                    "gender": person["gender"],
                    "place": label,
                    "scope": scope,
                    "problem": person["problem"],
                }
            )
        return Response({"kind": found["kind"], "people": people, "notes": found["notes"]})


ADDED = inline_serializer(
    "PeopleAdded",
    {
        "results": inline_serializer(
            "PersonAdded",
            {
                "line": serializers.IntegerField(allow_null=True),
                "outcome": serializers.ChoiceField(
                    choices=["registered", "asked", "already", "declined", "left", "problem"]
                ),
                "detail": serializers.CharField(),
            },
            many=True,
        )
    },
)


class AddPeopleView(_LedGroupView):
    """Add people: one typed in, or a batch of a checked list."""

    parser_classes = [JSONParser]

    @extend_schema(request=AddPeopleSerializer, responses=ADDED)
    def post(self, request, uuid):
        group = self.group(uuid)
        data = AddPeopleSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        results = services.add_people(
            actor=request.user,
            group=group,
            people=data.validated_data["people"],
            consent=data.validated_data["consent"],
        )
        return Response({"results": results})


class RemovePersonView(_LedGroupView):
    @extend_schema(request=None, responses=PlaceInGroupSerializer)
    def post(self, request, uuid, place):
        group = self.group(uuid)
        found = get_object_or_404(group.memberships.select_related("member", *PLACES), uuid=place)
        found = services.remove(actor=request.user, group=group, place=found)
        return Response(PlaceInGroupSerializer(found).data)


class GroupMessageView(_LedGroupView):
    """A message in the app to everyone in the group."""

    @extend_schema(
        request=GroupMessageSerializer,
        responses={
            201: inline_serializer("GroupMessageSent", {"people": serializers.IntegerField()})
        },
    )
    def post(self, request, uuid):
        group = self.group(uuid)
        data = GroupMessageSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        people = services.send_message(actor=request.user, group=group, **data.validated_data)
        return Response({"people": people}, status=status.HTTP_201_CREATED)


class GroupForCodeView(APIView):
    """For the joining page opened from a group's link: the group's name."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "delim_lookup"

    @extend_schema(
        parameters=[OpenApiParameter("code", str, required=True)],
        responses=inline_serializer("GroupForCode", {"name": serializers.CharField()}),
    )
    def get(self, request):
        group = services.group_for_code(request.query_params.get("code", ""))
        return Response({"name": group.name})


class OfferedKindsView(APIView):
    """The types of group someone registering an organization chooses from,
    on the joining page too, so no sign-in is needed."""

    permission_classes = [AllowAny]
    authentication_classes = []

    @extend_schema(
        responses=inline_serializer(
            "OfferedGroupKind",
            {"code": serializers.CharField(), "name": serializers.CharField()},
            many=True,
        )
    )
    def get(self, request):
        return Response(list(GroupKind.objects.filter(offered=True).values("code", "name")))
