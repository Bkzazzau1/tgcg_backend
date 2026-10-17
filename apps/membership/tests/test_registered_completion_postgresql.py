"""Registration cutoffs must be checked after real PostgreSQL row locks.

Every worker uses its own bounded connection, and all records are dummy data
created in pytest's isolated database. SQLite cannot exercise these races.
"""

import io
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection, connections, transaction
from django.utils import timezone
from PIL import Image

from apps.accounts.models import User
from apps.core.choices import GeographyLevel, MembershipState, Role, VerificationStatus
from apps.geography.models import PollingUnit, State
from apps.geography.polling_units import load_polling_units
from apps.membership import services
from apps.membership.models import Member, PvcUpdate
from conftest import PASSWORD, make_member

pytestmark = pytest.mark.django_db(transaction=True, serialized_rollback=True)

VIN = "90F5B0E8D0295847263"


@pytest.fixture(autouse=True)
def postgres_only(settings, tmp_path):
    if connection.vendor != "postgresql":
        pytest.skip("Real PostgreSQL connections and row locks are required.")
    settings.MEDIA_ROOT = tmp_path
    load_polling_units(lga_codes=["KD-ZARIA"])


@pytest.fixture
def registration():
    actor = User.objects.create_user(
        "REGISTER-PG", PASSWORD, full_name="Dummy Registrant", role=Role.MEMBER
    )
    account = User.objects.create_user(
        "MEMBER-PG",
        PASSWORD,
        full_name="Dummy Member",
        phone_number="+2348051234567",
        role=Role.MEMBER,
    )
    member = make_member(
        phone=account.phone_number,
        lga="KD-ZARIA",
        full_name=account.full_name,
        user=account,
        created_by=actor,
    )
    return actor, member


def picture(name):
    buffer = io.BytesIO()
    Image.new("RGB", (40, 40), (10, 36, 99)).save(buffer, "PNG")
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/png")


def pending_update(member):
    update = PvcUpdate(
        member=member,
        full_name="Dummy Card Member",
        vin=VIN,
        pvc_image=picture("pending-card.png"),
        scope_level=GeographyLevel.POLLING_UNIT,
        polling_unit=PollingUnit.objects.get(code="18-23-01-001"),
    )
    update.normalize_scope()
    update.full_clean()
    update.save()
    return update


def parallel(*jobs):
    barrier = threading.Barrier(len(jobs))

    def worker(job):
        try:
            connections.close_all()
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '5s'")
                cursor.execute("SELECT pg_backend_pid()")
                pid = cursor.fetchone()[0]
            barrier.wait(timeout=5)
            return pid, job(pid)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=len(jobs)) as executor:
        futures = [executor.submit(worker, job) for job in jobs]
        outcomes = [future.result(timeout=15) for future in futures]
    assert len({pid for pid, _ in outcomes}) == len(jobs)
    return [result for _, result in outcomes]


def wait_for_blocked_query(pid):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid = %s AND NOT granted)",
                [pid],
            )
            if cursor.fetchone()[0]:
                return
        time.sleep(0.01)
    pytest.fail("The overlapping request never reached a PostgreSQL lock wait.")


@pytest.mark.parametrize("operation", ["details", "selfie", "scan", "withdraw"])
@pytest.mark.parametrize("cutoff", ["verified", "rejected", "suspended"])
def test_registrant_cannot_write_after_a_cutoff_wins_the_member_lock(
    registration, operation, cutoff
):
    actor, member = registration
    update = pending_update(member) if operation == "withdraw" else None
    prechecked = threading.Event()
    cutoff_locked = threading.Event()
    request_pid = []

    def complete(pid):
        # This is the request's initial permission check, before its service
        # acquires the member lock. Its in-memory member will become stale.
        stale = Member.objects.select_related("user").get(pk=member.pk)
        services.require_completing(actor, stale)
        request_pid.append(pid)
        prechecked.set()
        assert cutoff_locked.wait(timeout=5)
        with pytest.raises(PermissionDenied):
            if operation == "details":
                services.update_own_details(member=stale, actor=actor, occupation="Trader")
            elif operation == "selfie":
                services.set_own_selfie(member=stale, actor=actor, upload=picture("selfie.png"))
            elif operation == "scan":
                services.scan_own_pvc(
                    member=stale,
                    actor=actor,
                    pvc_image=picture("card.png"),
                    full_name="Dummy Card Member",
                    vin=VIN,
                    delim="18/23/01/014",
                )
            else:
                services.withdraw_pvc_update(member=stale, actor=actor, update=update)

    def decide(_):
        assert prechecked.wait(timeout=5)
        with transaction.atomic():
            fresh = Member.objects.select_for_update().get(pk=member.pk)
            # Apply the coordinator's authorization cutoff while holding the
            # exact row that the completion service must subsequently lock.
            if cutoff == "suspended":
                fresh.membership_state = MembershipState.SUSPENDED
                fresh.save(update_fields=["membership_state"])
            else:
                fresh.verification_status = {
                    "verified": VerificationStatus.VERIFIED,
                    "rejected": VerificationStatus.REJECTED,
                }[cutoff]
                fresh.save(update_fields=["verification_status"])
            cutoff_locked.set()
            wait_for_blocked_query(request_pid[0])

    parallel(complete, decide)
    member.refresh_from_db()
    assert member.occupation == "" and not member.photo and not member.pvc_image
    assert member.full_name == "Dummy Member" and member.vin is None
    if cutoff == "suspended":
        assert member.membership_state == MembershipState.SUSPENDED
    else:
        assert (
            member.verification_status
            == {
                "verified": VerificationStatus.VERIFIED,
                "rejected": VerificationStatus.REJECTED,
            }[cutoff]
        )
    if update is not None:
        update.refresh_from_db()
        assert update.status == PvcUpdate.Status.PENDING
    else:
        assert not member.pvc_updates.exists()


def test_completed_first_sign_in_prevents_phone_and_password_correction(registration):
    actor, member = registration
    original_phone = member.phone_number
    original_password = member.user.password
    member_read = threading.Event()
    login_committed = threading.Event()
    signed_in_at = timezone.now()

    def correct(_):
        paused = False

        def pause_after_member_read(execute, sql, params, many, context):
            nonlocal paused
            result = execute(sql, params, many, context)
            if (
                not paused
                and sql.lstrip().upper().startswith("SELECT")
                and f'FROM "{Member._meta.db_table}"' in sql
                and "FOR UPDATE" in sql
            ):
                # The joined User snapshot has already been read. A cached
                # last_login is stale after the other connection commits.
                paused = True
                member_read.set()
                assert login_committed.wait(timeout=5)
            return result

        with connection.execute_wrapper(pause_after_member_read):
            with pytest.raises(ValidationError) as error:
                services.correct_registration(
                    actor=actor, member=member, phone_number="08057654321"
                )
        assert "phone_number" in error.value.message_dict
        assert paused, "Correction never acquired its real Member lock."

    def sign_in(_):
        assert member_read.wait(timeout=5)
        # Match Django's completed login marker, committed on a separate
        # connection before correction checks whether the number may change.
        assert User.objects.filter(pk=member.user_id).update(last_login=signed_in_at) == 1
        login_committed.set()

    parallel(correct, sign_in)
    member.refresh_from_db()
    member.user.refresh_from_db()
    assert member.phone_number == member.user.phone_number == original_phone
    assert member.user.password == original_password
    assert member.user.last_login == signed_in_at


def test_pvc_withdrawal_and_approval_use_member_before_update_lock_order(registration):
    _, member = registration
    member.verification_status = VerificationStatus.VERIFIED
    member.save(update_fields=["verification_status"])
    update = pending_update(member)
    reviewer = User.objects.create_user(
        "REVIEW-PG",
        PASSWORD,
        full_name="Dummy State Reviewer",
        role=Role.STATE_COORDINATOR,
        scope_level=GeographyLevel.STATE,
        state=State.objects.get(code="KD"),
    )
    member_locked = threading.Event()
    withdrawal_started = threading.Event()
    withdrawal_pid = []

    def approve(_):
        with transaction.atomic():
            Member.objects.select_for_update().get(pk=member.pk)
            member_locked.set()
            assert withdrawal_started.wait(timeout=5)
            wait_for_blocked_query(withdrawal_pid[0])
            # An update-first withdrawal would hold Update while waiting on
            # this Member, causing a real deadlock when approval continues.
            return services.approve_pvc_update(actor=reviewer, update=update).status

    def withdraw(pid):
        assert member_locked.wait(timeout=5)
        withdrawal_pid.append(pid)
        withdrawal_started.set()
        with pytest.raises(ValidationError) as error:
            services.withdraw_pvc_update(member=member, update=update)
        assert "already approved" in str(error.value)

    approved, _ = parallel(approve, withdraw)
    update.refresh_from_db()
    member.refresh_from_db()
    assert approved == update.status == PvcUpdate.Status.APPROVED
    assert member.full_name == update.full_name and member.vin == VIN
    assert member.pvc_image == update.pvc_image and update.decided_by_id == reviewer.pk
