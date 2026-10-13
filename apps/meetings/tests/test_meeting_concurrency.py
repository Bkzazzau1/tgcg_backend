"""Admission and closure must serialize on the same PostgreSQL meeting row."""

import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.core.exceptions import ValidationError
from django.db import connection, connections, transaction

from apps.accounts.models import User
from apps.core.choices import Role
from apps.meetings import livekit, services
from apps.meetings.models import Admission, Meeting
from conftest import PASSWORD

pytestmark = pytest.mark.django_db(transaction=True, serialized_rollback=True)


def wait_for_lock(pid):
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
    pytest.fail("Admission did not wait for the overlapping meeting row lock.")


@pytest.mark.parametrize("action", ["end", "cancel"])
def test_admission_waits_for_closure_and_cannot_issue_a_pass(action, settings, monkeypatch):
    if connection.vendor != "postgresql":
        pytest.skip("Real PostgreSQL connections and row locks are required.")
    settings.LIVEKIT_URL = "wss://meeting-test.invalid"
    settings.LIVEKIT_API_KEY = "test-key"
    settings.LIVEKIT_API_SECRET = "test-only-secret-long-enough-for-hs256"
    host = User.objects.create_user(
        "MEETING-HOST", PASSWORD, full_name="Meeting Host", role=Role.NATIONAL_ADMINISTRATOR
    )
    meeting = services.create_meeting(actor=host, title="Closure race")
    stale = Meeting.objects.get(pk=meeting.pk)
    closing = threading.Event()
    admitting_pid = queue.Queue()
    signed = []
    closed_rooms = []
    monkeypatch.setattr(livekit, "join_token", lambda **kwargs: signed.append(kwargs))
    monkeypatch.setattr(livekit, "close_room", lambda room: closed_rooms.append(room))

    def worker(job):
        try:
            connections.close_all()
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '5s'")
                cursor.execute("SELECT pg_backend_pid()")
                pid = cursor.fetchone()[0]
            return job(pid)
        finally:
            connections.close_all()

    def close(_):
        with transaction.atomic():
            Meeting.objects.select_for_update().get(pk=meeting.pk)
            if action == "end":
                services.end_meeting(actor=host, meeting=meeting)
            else:
                services.cancel_meeting(actor=host, meeting=meeting, reason="Race test")
            closing.set()
            # Admission has loaded an old object, but must now demonstrably
            # wait for this closing transaction before checking its status.
            wait_for_lock(admitting_pid.get(timeout=5))

    def admit(pid):
        assert closing.wait(timeout=5)
        admitting_pid.put(pid)
        with pytest.raises(ValidationError) as refused:
            services.admit(actor=host, meeting=stale)
        return str(refused.value)

    with ThreadPoolExecutor(max_workers=2) as executor:
        closing_result = executor.submit(worker, close)
        admitting_result = executor.submit(worker, admit)
        closing_result.result(timeout=15)
        refusal = admitting_result.result(timeout=15)

    assert ("ended" if action == "end" else "cancelled") in refusal
    assert signed == []
    assert not Admission.objects.exists()
    assert closed_rooms == [meeting.room_name]
