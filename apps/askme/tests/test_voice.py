"""Ask Me by voice (25 October 2026): Spitch writes spoken questions down and
reads answers aloud; recordings are kept only with the asker's consent."""

import json
from io import StringIO

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command

from apps.access.tests.test_access import client_for
from apps.askme.models import AskMeSettings, Question, SpokenAnswer

pytestmark = pytest.mark.django_db

VOICE = "/api/v1/askme/ask/voice/"
WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + bytes(28)
MP3 = b"ID3\x04" + bytes(60)


@pytest.fixture
def voice(monkeypatch, settings, tmp_path):
    """A stand-in for Spitch: hears what the test says, speaks MP3 bytes."""
    settings.SPITCH_API_KEY = "test-key"
    settings.MEDIA_ROOT = tmp_path
    calls = {"heard": [], "spoken": []}
    state = {"heard": "Me gwamnati ta yi wa dalibai?"}

    def transcribe(audio, *, filename, mime_type, language):
        calls["heard"].append({"bytes": len(audio), "mime": mime_type, "language": language})
        return state["heard"]

    def speak(text, *, language, voice):
        calls["spoken"].append({"text": text, "language": language, "voice": voice})
        return MP3

    monkeypatch.setattr("apps.askme.spitch.transcribe", transcribe)
    monkeypatch.setattr("apps.askme.spitch.speak", speak)
    return {"calls": calls, "state": state}


def speak_question(user, language="ha", name="question.wav", content=WAV):
    return client_for(user).post(
        VOICE,
        {"audio": SimpleUploadedFile(name, content, "audio/wav"), "language": language},
        format="multipart",
    )


def loans_answer(ai, library):
    ai["state"]["answer"] = {
        "status": "answered",
        "language": "ha",
        "answer": "Gwamnati ta ba dalibai 300,000 lamuni.",
        "cited": [library["loans"].pk],
    }


def test_a_spoken_question_is_heard_answered_and_read_aloud(
    member, library, open_ask_me, ai, voice
):
    loans_answer(ai, library)
    response = speak_question(member)
    assert response.status_code == 201, response.content
    body = response.json()
    assert body["channel"] == "voice"
    assert body["text"] == "Me gwamnati ta yi wa dalibai?"
    assert body["answer"] == "Gwamnati ta ba dalibai 300,000 lamuni."
    assert voice["calls"]["heard"] == [{"bytes": len(WAV), "mime": "audio/wav", "language": "ha"}]
    assert not Question.objects.get().recording  # not allowed to keep it

    heard = client_for(member).get(f"/api/v1/askme/questions/{body['id']}/audio/")
    assert heard.status_code == 200 and heard["Content-Type"] == "audio/mpeg"
    assert b"".join(heard.streaming_content) == MP3
    assert voice["calls"]["spoken"] == [
        {"text": "Gwamnati ta ba dalibai 300,000 lamuni.", "language": "ha", "voice": "aliyu"}
    ]
    # The same answer again is not spoken twice.
    client_for(member).get(f"/api/v1/askme/questions/{body['id']}/audio/")
    assert len(voice["calls"]["spoken"]) == 1 and SpokenAnswer.objects.count() == 1


def test_recordings_are_kept_only_with_consent_and_feed_training(
    member, reviewer, library, open_ask_me, ai, voice, tmp_path
):
    me = client_for(member).patch(
        "/api/v1/askme/me/", {"training_ok": True, "voice_ok": True}, format="json"
    )
    assert me.json()["voice_ok"] is True and me.json()["voice_ready"] is True
    loans_answer(ai, library)
    asked = speak_question(member).json()
    question = Question.objects.get()
    assert question.recording and question.training_ok

    queued = client_for(reviewer).get("/api/v1/askme/review/").json()["results"][0]
    assert queued["channel"] == "voice" and queued["has_recording"] is True
    played = client_for(reviewer).get(f"/api/v1/askme/review/{asked['id']}/recording/")
    assert played.status_code == 200 and b"".join(played.streaming_content) == WAV
    client_for(reviewer).post(
        f"/api/v1/askme/review/{asked['id']}/decide/",
        {"decision": "approve", "corrected_text": "Me gwamnati ta yi wa ɗalibai?"},
        format="json",
    )

    out, audio_dir = tmp_path / "training.jsonl", tmp_path / "audio"
    stdout = StringIO()
    call_command(
        "export_askme_training", "--out", str(out), "--audio-dir", str(audio_dir), stdout=stdout
    )
    assert "1 reviewed answers and 1 recordings" in stdout.getvalue()
    record = json.loads(out.read_text(encoding="utf-8"))
    assert record["question"] == "Me gwamnati ta yi wa ɗalibai?"
    assert record["heard"] == "Me gwamnati ta yi wa dalibai?"
    assert (audio_dir / record["audio"]).read_bytes() == WAV


def test_without_the_key_voice_is_unavailable(member, open_ask_me, settings):
    settings.SPITCH_API_KEY = ""
    assert speak_question(member).status_code == 503
    assert client_for(member).get("/api/v1/askme/me/").json()["voice_ready"] is False


def test_while_ask_me_is_off_nothing_is_sent_to_the_voice_service(member, voice):
    body = speak_question(member).json()
    assert body["status"] == "off"
    assert voice["calls"]["heard"] == []


def test_unusable_recordings_are_refused(member, library, open_ask_me, ai, voice):
    assert speak_question(member, name="question.txt").status_code == 400
    voice["state"]["heard"] = ""
    nothing = speak_question(member)
    assert nothing.status_code == 400 and "audio" in nothing.json()
    assert speak_question(member, language="").status_code == 400


def test_the_administrator_chooses_each_languages_voice(
    admin, member, library, open_ask_me, ai, voice
):
    options = client_for(admin).get("/api/v1/askme/settings/").json()
    assert options["voice_options"]["ha"]["chosen"] == "aliyu"
    assert "amina" in options["voice_options"]["ha"]["voices"]
    saved = client_for(admin).patch(
        "/api/v1/askme/settings/", {"voices": {"ha": "amina"}}, format="json"
    )
    assert saved.status_code == 200, saved.content
    bad = client_for(admin).patch(
        "/api/v1/askme/settings/", {"voices": {"ha": "nobody"}}, format="json"
    )
    assert bad.status_code == 400

    loans_answer(ai, library)
    asked = speak_question(member).json()
    client_for(member).get(f"/api/v1/askme/questions/{asked['id']}/audio/")
    assert voice["calls"]["spoken"][0]["voice"] == "amina"

    AskMeSettings.objects.filter(pk=1).update(voice_enabled=False)
    assert speak_question(member).status_code == 503
