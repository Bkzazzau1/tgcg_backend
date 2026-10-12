import pytest
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.core.choices import GeographyLevel, Role
from apps.geography.models import Lga

PASSWORD = "Strong-Pass-2027!"


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def make_user(db):
    def _make(access_id="TGCG-NA-0001", **extra):
        extra.setdefault("full_name", "Test Operator")
        return User.objects.create_user(access_id, PASSWORD, **extra)

    return _make


@pytest.fixture
def lga_coordinator(make_user):
    return make_user(
        "TGCG-KD-0001",
        full_name="Amina Kaduna",
        phone_number="08031234567",
        role=Role.LGA_COORDINATOR,
        scope_level=GeographyLevel.LGA,
        lga=Lga.objects.get(code="KD-KADUNA-NORTH"),
    )


@pytest.fixture
def auth_client(api_client, make_user):
    api_client.force_authenticate(make_user())
    return api_client


def verified_member(user, phone=None):
    """Make `user` the account of a member whose identity is verified, in the
    account's own area. Agents and observers act only then."""
    from apps.core.choices import VerificationStatus
    from apps.core.models import Sequence
    from apps.geography.scope import SCOPE_FIELDS
    from apps.membership.models import Member

    member = Member(
        full_name=user.full_name or user.access_id,
        phone_number=phone or user.phone_number or f"0809{user.pk:07d}",
        scope_level=user.scope_level,
        **{field: getattr(user, field) for field in SCOPE_FIELDS},
        membership_number=f"TGCG-{Sequence.next_value('member'):06d}",
        verification_status=VerificationStatus.VERIFIED,
        user=user,
    )
    member.full_clean()
    member.save()
    return member


def make_member(phone="08031112222", lga="KD-KADUNA-NORTH", **extra):
    """A registered member, created directly (registration flows have their own tests)."""
    from apps.core.models import Sequence
    from apps.membership.models import Member

    member = Member(
        full_name=extra.pop("full_name", "Musa Bello"),
        phone_number=phone,
        scope_level=GeographyLevel.LGA,
        lga=Lga.objects.get(code=lga),
        membership_number=f"TGCG-{Sequence.next_value('member'):06d}",
        **extra,
    )
    member.full_clean()
    member.save()
    return member
