"""Real PostgreSQL row-lock races, using only an isolated pytest database.

SQLite cannot exercise these locks. Each worker owns a separate connection;
lock waits are bounded and every connection is closed even after a failure.
"""

import io
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.db import connection, connections, transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.geography.polling_units import load_polling_units
from apps.membership import ussd
from apps.membership.models import Member, UssdSession
from apps.membership.models import UssdRegistrationDraft as Draft

pytestmark = pytest.mark.django_db(transaction=True, serialized_rollback=True)

PHONE = "+2348000000001"
VIN = "A" * 18 + "1"
UNIT = "18-23-01-001"
PIN = "482915"
CODE = "*384*1234#"


@pytest.fixture(autouse=True)
def postgres_only(settings):
    if connection.vendor != "postgresql":
        pytest.skip("Real PostgreSQL connections and row locks are required.")
    settings.USSD_SERVICE_CODE = CODE
    settings.USSD_DRAFT_TTL_DAYS = 30
    load_polling_units(lga_codes=["KD-ZARIA"])


def reply(session, *answers):
    return ussd.respond(
        phone=PHONE, session_id=session, service_code=CODE, text="*".join(answers)
    ).render()


def prepare_join(session="join"):
    answers = ["1", VIN, UNIT]
    for count in range(1, len(answers) + 1):
        assert reply(session, *answers[:count]).startswith("CON ")
    saved = Draft.objects.get(phone_number=PHONE)
    assert saved.step == Draft.Step.CONFIRM
    assert not Member.objects.exists()
    return saved


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


def test_identical_final_join_callbacks_create_one_member():
    saved = prepare_join()
    outcomes = parallel(
        lambda _: reply("join", "1", VIN, UNIT, "1"),
        lambda _: reply("join", "1", VIN, UNIT, "1"),
    )
    assert outcomes[0] == outcomes[1]
    assert outcomes[0].startswith("END Welcome to TGCG!")
    member = Member.objects.get(phone_number=PHONE)
    assert member.client_id == f"ussd:{saved.pk}" and member.vin == VIN
    assert member.polling_unit.code == UNIT
    assert Member.objects.count() == 1 and not User.objects.exists()
    assert not Draft.objects.exists()
    assert UssdSession.objects.get(provider_session_id="join").ended


def test_simultaneous_redial_and_completion_preserve_correct_progress():
    saved = prepare_join("old")
    final, opened = parallel(
        lambda _: reply("old", "1", VIN, UNIT, "1"),
        lambda _: reply("new", "1"),
    )
    assert opened.startswith("CON ")
    current = Draft.objects.get(phone_number=PHONE)
    if Member.objects.filter(phone_number=PHONE).exists():
        # Completion won; redial must recognize that membership and offer a profile.
        assert final.startswith("END Welcome to TGCG!")
        assert current.kind == Draft.Kind.PROFILE and current.step == Draft.Step.NAME
        assert current.vin == "" and current.polling_unit_id is None
    else:
        # Redial won; the old callback cannot overwrite its active saved progress.
        assert "newer session" in final.lower()
        assert current.pk == saved.pk and current.kind == Draft.Kind.JOIN
        assert current.step == Draft.Step.CONFIRM and current.vin == VIN
        assert current.polling_unit.code == UNIT
        assert reply("new", "1", "1").startswith("END Welcome to TGCG!")
    member = Member.objects.get(phone_number=PHONE)
    assert member.client_id == f"ussd:{saved.pk}" and member.vin == VIN
    assert Member.objects.count() == 1 and not User.objects.exists()


def test_identical_profile_completion_callbacks_create_one_user_without_reset():
    prepare_join()
    reply("join", "1", VIN, UNIT, "1")
    answers = ["1", "Test Member", PIN]
    for count in range(1, len(answers) + 1):
        assert reply("profile", *answers[:count]).startswith("CON ")
    encoded = Draft.objects.get(phone_number=PHONE).pin_hash
    outcomes = parallel(
        lambda _: reply("profile", *answers, PIN),
        lambda _: reply("profile", *answers, PIN),
    )
    assert outcomes[0] == outcomes[1]
    assert outcomes[0].startswith("END Profile complete!")
    member = Member.objects.get(phone_number=PHONE)
    assert member.full_name == "Test Member" and member.user.password == encoded
    assert member.user.check_password(PIN)
    assert Member.objects.count() == User.objects.count() == 1
    assert not Draft.objects.exists()
    assert reply("after-profile", "1").startswith("END ")
    member.user.refresh_from_db()
    assert member.user.password == encoded


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
    pytest.fail("Purge never reached a real overlapping PostgreSQL lock wait.")


def test_cleanup_and_callback_follow_the_same_lock_order():
    prepare_join()
    old_time = timezone.now() - timedelta(days=31)
    Draft.objects.filter(phone_number=PHONE).update(updated_at=old_time)
    UssdSession.objects.filter(phone_number=PHONE).update(updated_at=old_time)
    draft_locked = threading.Event()
    purge_pid = queue.Queue()

    def callback(_):
        with transaction.atomic():
            Draft.objects.select_for_update().get(phone_number=PHONE)
            draft_locked.set()
            wait_for_blocked_query(purge_pid.get(timeout=5))
            # Purge is demonstrably waiting on our draft lock. A session-first
            # purge would already hold the session row and deadlock this callback.
            return reply("join", "1", VIN, UNIT, "1")

    def purge(pid):
        assert draft_locked.wait(timeout=5)
        purge_pid.put(pid)
        output = io.StringIO()
        call_command("purge_ussd_progress", stdout=output)
        return output.getvalue()

    response, output = parallel(callback, purge)
    assert response.startswith("END Saved progress expired.")
    assert "Purged 1 USSD draft(s) and 1 session(s)" in output
    assert not Draft.objects.exists() and not UssdSession.objects.exists()
    assert not Member.objects.exists() and not User.objects.exists()
