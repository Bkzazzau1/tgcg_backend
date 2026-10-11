import pytest

from apps.accounts.models import User
from apps.core.choices import Role
from conftest import PASSWORD


@pytest.fixture
def admin():
    """A National Administrator with the former fixed role."""
    return User.objects.create_user(
        "NA-1", PASSWORD, full_name="National Admin", role=Role.NATIONAL_ADMINISTRATOR
    )
