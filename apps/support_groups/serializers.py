from rest_framework import serializers

from apps.accounts.phone import local_phone
from apps.core.choices import VerificationStatus
from apps.geography.scope import ScopeField

from .models import GroupMember, SupportGroup
from .places import place_of


class OfficialSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=150, allow_blank=True)
    position = serializers.CharField(max_length=150, required=False, allow_blank=True)
    phone = serializers.CharField(max_length=20, required=False, allow_blank=True)


class OrganizationDetailsSerializer(serializers.Serializer):
    """The organization's part of the registration form. Field names match
    those of joining, where they come in the same form as the leader's."""

    organization_name = serializers.CharField(max_length=200, allow_blank=True)
    organization_coverage = serializers.ChoiceField(choices=SupportGroup.Coverage.choices)
    organization_zone = serializers.CharField(
        max_length=4, required=False, allow_blank=True, help_text="Zone code, for a region."
    )
    organization_state = serializers.CharField(
        max_length=4, required=False, allow_blank=True, help_text="State code, for a state."
    )
    organization_email = serializers.CharField(max_length=254, required=False, allow_blank=True)
    organization_officials = serializers.JSONField(
        required=False, help_text="Up to three: [{name, position, phone}], as JSON text in a form."
    )

    def details(self) -> dict:
        data = self.validated_data
        officials = data.get("organization_officials") or []
        return {
            "name": data["organization_name"],
            "coverage": data["organization_coverage"],
            "zone": data.get("organization_zone", ""),
            "state": data.get("organization_state", ""),
            "email": data.get("organization_email", ""),
            "officials": officials if isinstance(officials, list) else [officials],
        }


class RegisterGroupSerializer(OrganizationDetailsSerializer):
    """A signed-in member registers an organization. Without a leader's phone
    they lead it themselves."""

    leader_full_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    leader_phone_number = serializers.CharField(max_length=20, required=False, allow_blank=True)
    consent = serializers.BooleanField(
        required=False, help_text="A new leader agreed to join TGCG (only for a new leader)."
    )


class _GroupFields(serializers.ModelSerializer):
    id = serializers.UUIDField(source="uuid", read_only=True)
    zone = serializers.CharField(source="zone.name", default=None, read_only=True)
    state = serializers.CharField(source="state.name", default=None, read_only=True)
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    coverage_label = serializers.CharField(source="get_coverage_display", read_only=True)
    leader = serializers.SerializerMethodField()
    members = serializers.IntegerField(read_only=True, default=None)
    approved_members = serializers.IntegerField(read_only=True, default=None)
    waiting = serializers.IntegerField(read_only=True, default=None)

    class Meta:
        model = SupportGroup
        fields = [
            "id",
            "serial_number",
            "name",
            "coverage",
            "coverage_label",
            "zone",
            "state",
            "email",
            "officials",
            "leader",
            "leader_confirmed",
            "status",
            "status_label",
            "decision_reason",
            "decided_at",
            "created_at",
            "members",
            "approved_members",
            "waiting",
        ]
        read_only_fields = fields

    leader_confirmed = serializers.SerializerMethodField()

    def get_leader(self, group) -> dict:
        leader = group.leader
        return {
            "membership_number": leader.membership_number,
            "full_name": leader.full_name,
            "phone_number": local_phone(leader.phone_number),
            "place": place_of(leader),
        }

    def get_leader_confirmed(self, group) -> bool:
        return group.leader_confirmed_at is not None


class VettedGroupSerializer(_GroupFields):
    """A group as those who vet groups see it."""

    registered_by = serializers.CharField(source="registered_by.full_name", default="")
    decided_by = serializers.CharField(source="decided_by.full_name", default="")

    class Meta(_GroupFields.Meta):
        fields = [*_GroupFields.Meta.fields, "registered_by", "decided_by"]
        read_only_fields = fields


class LedGroupSerializer(_GroupFields):
    """A group as its own leader sees it: with its code and link once approved."""

    join_code = serializers.SerializerMethodField()
    link = serializers.SerializerMethodField()

    class Meta(_GroupFields.Meta):
        fields = [*_GroupFields.Meta.fields, "join_code", "link"]
        read_only_fields = fields

    def get_join_code(self, group) -> str | None:
        return group.join_code if group.status == SupportGroup.Status.APPROVED else None

    def get_link(self, group) -> str | None:
        from .services import link_for

        if group.status != SupportGroup.Status.APPROVED or not group.join_code:
            return None
        return link_for(group)


class PlaceInGroupSerializer(serializers.ModelSerializer):
    """Someone in a group, as its leader sees them: only their name, phone,
    place and whether TGCG approved them; never their PVC, photos or other
    details. Until someone already registered says yes, only the name and
    number the group gave."""

    id = serializers.UUIDField(source="uuid", read_only=True)
    full_name = serializers.SerializerMethodField()
    phone_number = serializers.SerializerMethodField()
    place = serializers.SerializerMethodField()
    approved = serializers.SerializerMethodField()
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    via_label = serializers.CharField(source="get_via_display", read_only=True)

    class Meta:
        model = GroupMember
        fields = [
            "id",
            "full_name",
            "phone_number",
            "place",
            "approved",
            "status",
            "status_label",
            "via",
            "via_label",
            "created_at",
            "answered_at",
        ]
        read_only_fields = fields

    def _shown(self, place) -> bool:
        return place.status == GroupMember.Status.ACTIVE

    def get_full_name(self, place) -> str:
        return place.member.full_name if self._shown(place) else place.given_name

    def get_phone_number(self, place) -> str:
        return local_phone(place.member.phone_number)

    def get_place(self, place) -> str:
        return place_of(place.member) if self._shown(place) else ""

    def get_approved(self, place) -> bool | None:
        if not self._shown(place):
            return None
        return place.member.verification_status == VerificationStatus.VERIFIED


class VettedPlaceSerializer(PlaceInGroupSerializer):
    """The same for those who vet groups, with the membership number to open
    the member's full record, which their own powers then govern."""

    membership_number = serializers.CharField(source="member.membership_number")

    class Meta(PlaceInGroupSerializer.Meta):
        fields = [*PlaceInGroupSerializer.Meta.fields, "membership_number"]
        read_only_fields = fields

    def _shown(self, place) -> bool:
        return True


class MyPlaceSerializer(serializers.ModelSerializer):
    """A group the signed-in member is in, or is asked to confirm."""

    id = serializers.UUIDField(source="uuid", read_only=True)
    group = serializers.SerializerMethodField()
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    leads = serializers.SerializerMethodField()

    class Meta:
        model = GroupMember
        fields = ["id", "group", "status", "status_label", "via", "leads", "created_at"]
        read_only_fields = fields

    def get_group(self, place) -> dict:
        group = place.group
        return {
            "id": str(group.uuid),
            "serial_number": group.serial_number,
            "name": group.name,
            "coverage_label": group.get_coverage_display(),
            "leader": group.leader.full_name,
            "status": group.status,
        }

    def get_leads(self, place) -> bool:
        return place.via == GroupMember.Via.LEADER


class PersonToAddSerializer(serializers.Serializer):
    line = serializers.IntegerField(required=False, allow_null=True)
    full_name = serializers.CharField(max_length=150, allow_blank=True)
    phone_number = serializers.CharField(max_length=20)
    scope = ScopeField(required=False, allow_null=True)
    gender = serializers.ChoiceField(choices=["male", "female"], required=False, allow_blank=True)

    def validate_scope(self, scope):
        if not scope:
            return None
        if scope.get("scope_level") == "country":
            return None
        return scope


class AddPeopleSerializer(serializers.Serializer):
    people = PersonToAddSerializer(many=True)
    consent = serializers.BooleanField(
        help_text="These people agreed to join TGCG and to be listed in this group."
    )


class GroupMessageSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=80)
    body = serializers.CharField(max_length=1000)


class GroupReasonSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=1000, required=False, allow_blank=True)


class AnswerSerializer(serializers.Serializer):
    yes = serializers.BooleanField()


class CodeSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=12)
