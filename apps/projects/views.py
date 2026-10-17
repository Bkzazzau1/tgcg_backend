from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.choices import GeographyLevel
from apps.core.permissions import Capability, has_capability
from apps.geography.scope import SCOPE_FIELDS, SCOPE_SELECT_RELATED
from apps.membership.models import Member
from apps.membership.registration_views import IsMember

from . import monitor, services
from .models import Participation, Project
from .serializers import (
    AssignSerializer,
    ParticipationSerializer,
    ProjectReasonSerializer,
    ProjectSerializer,
    ProjectUpdateSerializer,
    ProjectWriteSerializer,
)

C = Capability


def _require_watching(user):
    if not has_capability(user, *monitor.WATCHING):
        raise PermissionDenied("Your role does not include watching project work.")


def default_place(member) -> dict:
    """Where someone works unless the coordinator says otherwise: the polling
    unit of their role, or else where they are registered."""
    from apps.tracking.services import duty_polling_unit

    unit = duty_polling_unit(member.user) if member.user_id else None
    if unit is not None:
        return {"scope_level": GeographyLevel.POLLING_UNIT, "polling_unit": unit}
    return {"scope_level": member.scope_level, **{f: getattr(member, f) for f in SCOPE_FIELDS}}


class ProjectViewSet(mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Projects: scheduled periods of field work, such as an election.

    Everyone with a role sees the projects in their areas (scheduled, on now,
    or finished within a week); a National Administrator sees and manages all
    of them. Coordinators assign people to a project and watch where they are
    (`participants/`, `monitor/`).
    """

    serializer_class = ProjectSerializer
    lookup_field = "uuid"
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Project.objects.select_related("election").prefetch_related("areas")

    def _project(self, request):
        project = self.get_object()
        services.require_visible(request.user, project)
        return project

    def _data(self, project, code=status.HTTP_200_OK):
        project = self.get_queryset().get(pk=project.pk)
        return Response(ProjectSerializer(project, context={"request": self.request}).data, code)

    def list(self, request):
        projects = services.visible_projects(request.user)
        return Response(ProjectSerializer(projects, many=True, context={"request": request}).data)

    def retrieve(self, request, uuid=None):
        return self._data(self._project(request))

    @extend_schema(request=ProjectWriteSerializer, responses={201: ProjectSerializer})
    def create(self, request):
        data = ProjectWriteSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        project = services.create_project(actor=request.user, **data.validated_data)
        return self._data(project, status.HTTP_201_CREATED)

    @extend_schema(request=ProjectUpdateSerializer, responses=ProjectSerializer)
    def partial_update(self, request, uuid=None):
        data = ProjectUpdateSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        project = services.update_project(
            actor=request.user, project=self.get_object(), **data.validated_data
        )
        return self._data(project)

    @extend_schema(request=ProjectReasonSerializer, responses=ProjectSerializer)
    @action(detail=True, methods=["post"])
    def cancel(self, request, uuid=None):
        data = ProjectReasonSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        project = services.cancel_project(
            actor=request.user,
            project=self.get_object(),
            reason=data.validated_data.get("reason", ""),
        )
        return self._data(project)

    @extend_schema(methods=["GET"], responses=ParticipationSerializer(many=True), request=None)
    @extend_schema(
        methods=["POST"], request=AssignSerializer, responses={201: ParticipationSerializer}
    )
    @action(detail=True, methods=["get", "post"])
    def participants(self, request, uuid=None):
        """GET: the people in the project in your areas. POST: assign someone
        with a role (`member`), at `place` (by default their own polling unit)."""
        project = self._project(request)
        if request.method == "POST":
            data = AssignSerializer(data=request.data)
            data.is_valid(raise_exception=True)
            member = Member.objects.filter(membership_number=data.validated_data["member"]).first()
            if member is None:
                raise ValidationError({"member": "No member has this membership number."})
            participation = services.assign(
                actor=request.user,
                project=project,
                member=member,
                place=data.validated_data.get("place") or default_place(member),
            )
            return Response(
                ParticipationSerializer(participation).data, status=status.HTTP_201_CREATED
            )
        _require_watching(request.user)
        people = (
            Participation.objects.filter(project=project)
            .filter(monitor.watched_q(request.user))
            .select_related("project", "member", "assigned_by", *SCOPE_SELECT_RELATED)
            .order_by("member__full_name", "id")
        )
        if wanted := request.query_params.get("status"):
            people = people.filter(status=wanted)
        return Response(ParticipationSerializer(people, many=True).data)

    @extend_schema(responses={200: OpenApiTypes.OBJECT})
    @action(detail=True, methods=["get"])
    def monitor(self, request, uuid=None):
        """Each person's state in your areas (at their place, away, not sharing
        their location, waiting for their answer...) and the project's work there."""
        project = self._project(request)
        _require_watching(request.user)
        return Response(monitor.presence(project, request.user))


class ParticipationViewSet(viewsets.GenericViewSet):
    """A person's place in a project; `remove/` takes them out (reason required)."""

    serializer_class = ParticipationSerializer
    lookup_field = "uuid"
    queryset = Participation.objects.select_related(
        "project", "member", "assigned_by", *SCOPE_SELECT_RELATED
    )

    @extend_schema(request=ProjectReasonSerializer, responses=ParticipationSerializer)
    @action(detail=True, methods=["post"])
    def remove(self, request, uuid=None):
        data = ProjectReasonSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        participation = services.remove(
            actor=request.user,
            participation=self.get_object(),
            reason=data.validated_data.get("reason", ""),
        )
        return Response(ParticipationSerializer(self.get_queryset().get(pk=participation.pk)).data)


class MyProjectsView(APIView):
    """The projects the member is asked to, or does, take part in: invitations
    to answer, the one on now, and those coming up or just finished."""

    permission_classes = [IsMember]

    @extend_schema(responses=ProjectSerializer(many=True))
    def get(self, request):
        recent = timezone.now() - services.RECENT
        projects = (
            Project.objects.filter(participants__member=request.user.member, ends_at__gt=recent)
            .select_related("election")
            .prefetch_related("areas")
            .distinct()
            .order_by("starts_at", "id")
        )
        return Response(ProjectSerializer(projects, many=True, context={"request": request}).data)


ANSWERS = {
    "accept": lambda user, p, reason: services.respond(
        user=user, participation=p, accept=True, reason=reason
    ),
    "decline": lambda user, p, reason: services.respond(
        user=user, participation=p, accept=False, reason=reason
    ),
    "withdraw": lambda user, p, reason: services.withdraw(
        user=user, participation=p, reason=reason
    ),
}


class MyProjectAnswerView(APIView):
    """`accept` (agreeing to share your location while the project runs),
    `decline`, or `withdraw` (stop taking part)."""

    permission_classes = [IsMember]

    @extend_schema(request=ProjectReasonSerializer, responses=ParticipationSerializer)
    def post(self, request, uuid, answer):
        if answer not in ANSWERS:
            raise ValidationError("Answer accept, decline or withdraw.")
        data = ProjectReasonSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        participation = get_object_or_404(Participation, uuid=uuid, member=request.user.member)
        participation = ANSWERS[answer](
            request.user, participation, data.validated_data.get("reason", "")
        )
        participation = Participation.objects.select_related(
            "project", "member", "assigned_by", *SCOPE_SELECT_RELATED
        ).get(pk=participation.pk)
        return Response(ParticipationSerializer(participation).data)
