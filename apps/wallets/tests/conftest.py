import pytest

from apps.access.tests.test_access import KD, KN, appoint, client_for, custom_role, member_account
from apps.accounts.models import User
from apps.core.choices import Role
from apps.core.permissions import Capability
from conftest import PASSWORD

C = Capability
FINANCE = [
    C.VIEW_WALLETS,
    C.CREDIT_WALLETS,
    C.APPROVE_WALLET_CREDITS,
    C.APPROVE_WITHDRAWALS,
]


@pytest.fixture
def admin():
    return User.objects.create_user(
        "NA-1", PASSWORD, full_name="National Admin", role=Role.NATIONAL_ADMINISTRATOR
    )


@pytest.fixture
def finance(admin):
    """Finance officers: two in Kaduna, one in Kano."""
    role = custom_role(admin, "Finance Officer", FINANCE)

    def officer(phone, name, area):
        member = member_account(phone, name=name)
        assert appoint(admin, member, role, [area]).status_code == 201
        return client_for(member.user)

    return {
        "kaduna": officer("08035550001", "Kaduna Finance One", KD),
        "kaduna2": officer("08035550002", "Kaduna Finance Two", KD),
        "kano": officer("08035550003", "Kano Finance", KN),
    }
