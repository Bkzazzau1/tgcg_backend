"""Reading PVC photos sent from a browser (apps.membership.pvc_reading)."""

import base64
import io
import json
import shutil
import subprocess

import pytest
from PIL import Image, ImageDraw, ImageFont
from rest_framework.test import APIClient

from apps.membership import pvc_reading

pytestmark = pytest.mark.django_db

URL = "/api/v1/register/read-pvc/"
# What a reader might return for a card. Made up: no real holder's details here.
CARD_TEXT = "CODE: 18-23-01-001 VIN 90F5 B0E8 D029 5847 263\nBELLO, MUSA IBRAHIM\n"


def photo(size=(900, 560), orientation=None):
    image = Image.new("RGB", size, (214, 236, 222))
    buffer = io.BytesIO()
    exif = Image.Exif()
    if orientation:
        exif[0x0112] = orientation
    image.save(buffer, "JPEG", exif=exif)
    buffer.seek(0)
    buffer.name = "pvc.jpg"
    return buffer


def send(image=None):
    return APIClient().post(URL, {"image": image or photo()}, format="multipart")


@pytest.fixture
def no_google(settings):
    settings.GOOGLE_VISION_API_KEY = ""


@pytest.fixture
def tesseract(settings, monkeypatch, no_google):
    """Tesseract "installed", answering with CARD_TEXT; records what it was given."""
    settings.TESSERACT_CMD = "tesseract"
    monkeypatch.setattr(pvc_reading.shutil, "which", lambda cmd: "/usr/bin/tesseract")
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout=CARD_TEXT.encode(), stderr=b"")

    monkeypatch.setattr(pvc_reading.subprocess, "run", run)
    return calls


def test_without_an_engine_browsers_type_the_details(settings, monkeypatch, no_google):
    monkeypatch.setattr(pvc_reading.shutil, "which", lambda cmd: None)
    response = send()
    assert response.status_code == 503
    assert "Type the details" in response.json()["detail"]


def test_tesseract_reads_a_grey_enlarged_copy(tesseract):
    response = send()
    assert response.status_code == 200
    assert response.json() == {"text": CARD_TEXT}
    args, kwargs = tesseract[0]
    assert args == ["tesseract", "stdin", "stdout", "-l", "eng", "--psm", "4"]
    sent = Image.open(io.BytesIO(kwargs["input"]))
    assert sent.mode == "L" and sent.width == pvc_reading.TESSERACT_WIDTH
    assert kwargs["timeout"] == pvc_reading.TIMEOUT_SECONDS


def test_large_photos_shrink_only_so_far(tesseract):
    send(photo(size=(2600, 1600)))  # kept as it is
    send(photo(size=(6000, 4000)))  # the card on a table, from a big camera
    widths = [Image.open(io.BytesIO(kwargs["input"])).width for _, kwargs in tesseract]
    assert widths == [2600, pvc_reading.TESSERACT_MAX_WIDTH]


def test_a_photo_saved_sideways_is_turned_upright(tesseract):
    send(photo(size=(900, 560), orientation=6))  # 6: rotate 90 degrees to view
    sent = Image.open(io.BytesIO(tesseract[0][1]["input"]))
    assert sent.height > sent.width


def test_a_failed_read_says_to_type_the_details(tesseract, monkeypatch):
    def broken(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(pvc_reading.subprocess, "run", broken)
    response = send()
    assert response.status_code == 503
    assert "could not be read" in response.json()["detail"]


def test_only_photos_are_read(tesseract):
    not_a_photo = io.BytesIO(b"%PDF-1.4 not an image")
    not_a_photo.name = "card.pdf"
    assert send(not_a_photo).status_code == 400
    assert APIClient().post(URL, {}, format="multipart").status_code == 400
    assert not tesseract


def test_google_vision_is_used_when_a_key_is_set(settings, monkeypatch, tesseract):
    settings.GOOGLE_VISION_API_KEY = "test-key"
    seen = {}

    class Reply(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def urlopen(request, timeout):
        seen["request"], seen["timeout"] = request, timeout
        body = {"responses": [{"fullTextAnnotation": {"text": CARD_TEXT}}]}
        return Reply(json.dumps(body).encode())

    monkeypatch.setattr(pvc_reading.urllib.request, "urlopen", urlopen)
    response = send(photo(size=(4000, 2500)))
    assert response.json() == {"text": CARD_TEXT}
    assert not tesseract  # A successful Google read makes no local request.

    request = seen["request"]
    assert request.full_url == pvc_reading.GOOGLE_VISION_URL  # the key is not in the URL
    assert request.get_header("X-goog-api-key") == "test-key"
    payload = json.loads(request.data)["requests"][0]
    assert payload["features"] == [{"type": "TEXT_DETECTION"}]
    sent = Image.open(io.BytesIO(base64.b64decode(payload["image"]["content"])))
    assert max(sent.size) == pvc_reading.GOOGLE_MAX_SIDE


def test_a_google_error_says_to_type_the_details(settings, monkeypatch):
    settings.GOOGLE_VISION_API_KEY = "test-key"
    monkeypatch.setattr(pvc_reading.shutil, "which", lambda cmd: None)

    def refused(request, timeout):
        raise pvc_reading.urllib.error.URLError("unreachable")

    monkeypatch.setattr(pvc_reading.urllib.request, "urlopen", refused)
    assert send().status_code == 503


def google_reply(monkeypatch, body):
    def urlopen(request, timeout):
        return io.BytesIO(json.dumps(body).encode())

    monkeypatch.setattr(pvc_reading.urllib.request, "urlopen", urlopen)


@pytest.mark.parametrize(
    "error",
    [
        pvc_reading.urllib.error.URLError("unreachable"),
        TimeoutError("timed out"),
        ConnectionResetError("connection reset"),
        pvc_reading.http.client.RemoteDisconnected("provider closed connection"),
        pvc_reading.urllib.error.HTTPError(
            pvc_reading.GOOGLE_VISION_URL, 429, "quota exceeded", None, None
        ),
    ],
)
def test_google_request_failures_use_tesseract(settings, monkeypatch, tesseract, error):
    settings.GOOGLE_VISION_API_KEY = "test-key"

    def refused(request, timeout):
        raise error

    monkeypatch.setattr(pvc_reading.urllib.request, "urlopen", refused)
    response = send()
    assert response.status_code == 200
    assert response.json() == {"text": CARD_TEXT}
    assert len(tesseract) == 1


def test_google_truncated_reply_uses_tesseract_without_logging_photo_text(
    settings, monkeypatch, tesseract, caplog
):
    settings.GOOGLE_VISION_API_KEY = "test-key"

    class DroppedReply(io.BytesIO):
        def read(self, *args, **kwargs):
            raise pvc_reading.http.client.IncompleteRead(CARD_TEXT.encode(), 200)

    monkeypatch.setattr(
        pvc_reading.urllib.request, "urlopen", lambda request, timeout: DroppedReply()
    )
    response = send()
    assert response.status_code == 200
    assert response.json() == {"text": CARD_TEXT}
    assert len(tesseract) == 1
    assert CARD_TEXT not in caplog.text
    assert "test-key" not in caplog.text


def test_google_provider_error_uses_tesseract_without_logging_data(
    settings, monkeypatch, tesseract, caplog
):
    settings.GOOGLE_VISION_API_KEY = "test-key"
    google_reply(
        monkeypatch,
        {"responses": [{"error": {"code": 429, "status": CARD_TEXT, "message": "test-key"}}]},
    )
    response = send()
    assert response.status_code == 200
    assert response.json() == {"text": CARD_TEXT}
    assert len(tesseract) == 1
    assert CARD_TEXT not in caplog.text
    assert "test-key" not in caplog.text
    assert "pvc.jpg" not in caplog.text


@pytest.mark.parametrize("text", ["", " \n\t "])
def test_google_no_text_uses_tesseract(settings, monkeypatch, tesseract, text):
    settings.GOOGLE_VISION_API_KEY = "test-key"
    google_reply(monkeypatch, {"responses": [{"fullTextAnnotation": {"text": text}}]})
    response = send()
    assert response.status_code == 200
    assert response.json() == {"text": CARD_TEXT}
    assert len(tesseract) == 1


@pytest.mark.parametrize(
    "body",
    [
        None,
        {"responses": {}},
        {"responses": []},
        {"responses": [None]},
        {"responses": [{"fullTextAnnotation": None}]},
        {"responses": [{"fullTextAnnotation": {"text": 123}}]},
    ],
)
def test_google_malformed_reply_uses_tesseract(settings, monkeypatch, tesseract, body):
    settings.GOOGLE_VISION_API_KEY = "test-key"
    google_reply(monkeypatch, body)
    response = send()
    assert response.status_code == 200
    assert response.json() == {"text": CARD_TEXT}
    assert len(tesseract) == 1


@pytest.mark.parametrize("text", ["", " \n\t "])
def test_google_no_text_without_tesseract_says_to_type_details(settings, monkeypatch, text):
    settings.GOOGLE_VISION_API_KEY = "test-key"
    monkeypatch.setattr(pvc_reading.shutil, "which", lambda cmd: None)
    google_reply(monkeypatch, {"responses": [{"fullTextAnnotation": {"text": text}}]})
    response = send()
    assert response.status_code == 503
    assert "could not be read" in response.json()["detail"]


def test_both_readers_failing_says_to_type_details(settings, monkeypatch, tesseract):
    settings.GOOGLE_VISION_API_KEY = "test-key"
    google_reply(monkeypatch, {"responses": [{"error": {"status": "RESOURCE_EXHAUSTED"}}]})

    def broken(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(pvc_reading.subprocess, "run", broken)
    response = send()
    assert response.status_code == 503
    assert "could not be read" in response.json()["detail"]


def test_google_fallback_preserves_the_full_size_photo(settings, monkeypatch, tesseract):
    settings.GOOGLE_VISION_API_KEY = "test-key"

    def shrinking_read(image):
        image.thumbnail((pvc_reading.GOOGLE_MAX_SIDE, pvc_reading.GOOGLE_MAX_SIDE))
        raise pvc_reading.ReadFailed

    monkeypatch.setattr(pvc_reading, "_google", shrinking_read)
    response = send(photo(size=(4000, 2500)))
    assert response.status_code == 200
    sent = Image.open(io.BytesIO(tesseract[0][1]["input"]))
    assert sent.size == (pvc_reading.TESSERACT_MAX_WIDTH, 1875)


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="Tesseract is not installed")
def test_tesseract_reads_printed_card_text(settings, no_google):
    settings.TESSERACT_CMD = "tesseract"
    image = Image.new("RGB", (1200, 400), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=48)
    draw.text((40, 60), "CODE: 18-23-01-001", fill="black", font=font)
    draw.text((40, 200), "BELLO, MUSA IBRAHIM", fill="black", font=font)
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    buffer.seek(0)
    buffer.name = "card.png"
    text = send(buffer).json()["text"]
    assert "18-23-01-001" in text and "BELLO" in text
