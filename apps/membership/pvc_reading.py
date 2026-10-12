"""Reading the text on a PVC photo, for registrations from a browser.

The Android and iPhone apps read the card on the phone. A browser cannot, so
it sends the photo here and gets the text back; the app picks out the fields
exactly as the phones do, and the member checks each one. Reading a card is
not validating it.

The engine depends on configuration:
- Google Cloud Vision first, when GOOGLE_VISION_API_KEY is set. The photo
  goes to Google, including when the request later uses the local reader.
- Tesseract, when installed, if Google fails or returns no text, or if no
  Google key is configured. It runs on this server but often misreads the
  VIN and polling unit code on the card's patterned background.

One reader's text is returned; the two readings are never combined. A
non-empty reading still needs to be checked against the card.

Nothing is stored, and neither the photo nor the text is ever logged.
"""

import base64
import http.client
import io
import json
import logging
import shutil
import subprocess
import urllib.error
import urllib.request

from django.conf import settings
from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

GOOGLE_VISION_URL = "https://vision.googleapis.com/v1/images:annotate"
TIMEOUT_SECONDS = 25
# Card text is small: Tesseract reads it best on a photo at least this wide.
# Larger photos (often the card on a table) shrink only to the upper limit,
# so the text on the card itself stays big enough.
TESSERACT_WIDTH = 2000
TESSERACT_MAX_WIDTH = 3000
# Google needs no more than this, and a smaller photo uploads faster.
GOOGLE_MAX_SIDE = 2048


class ReaderUnavailable(Exception):
    """No engine is configured on this server."""


class ReadFailed(Exception):
    """The engine could not read the photo (an error or a timeout)."""


def engine() -> str | None:
    if settings.GOOGLE_VISION_API_KEY:
        return "google"
    if shutil.which(settings.TESSERACT_CMD):
        return "tesseract"
    return None


def read_text(upload) -> str:
    """The text on a PVC photo (an upload already checked to be an image)."""
    chosen = engine()
    if chosen is None:
        raise ReaderUnavailable
    image = Image.open(upload)
    image.load()
    image = ImageOps.exif_transpose(image)  # phones save photos sideways with a rotation tag
    if chosen != "google":
        return _tesseract(image)
    try:
        # Keep the upright, full-size photo for the local reader if needed.
        text = _google(image.copy())
    except ReadFailed:
        if not shutil.which(settings.TESSERACT_CMD):
            raise
    else:
        if text.strip():
            return text
        if not shutil.which(settings.TESSERACT_CMD):
            raise ReadFailed
    return _tesseract(image)


def prepare_for_tesseract(image: Image.Image) -> Image.Image:
    """Grey, 2000 to 3000 px wide and at full contrast: what Tesseract reads best."""
    grey = ImageOps.grayscale(image)
    width = min(max(grey.width, TESSERACT_WIDTH), TESSERACT_MAX_WIDTH)
    if width != grey.width:
        height = max(1, round(grey.height * width / grey.width))
        grey = grey.resize((width, height), Image.Resampling.LANCZOS)
    return ImageOps.autocontrast(grey, cutoff=1)


def _tesseract(image: Image.Image) -> str:
    buffer = io.BytesIO()
    prepare_for_tesseract(image).save(buffer, "PNG")
    try:
        result = subprocess.run(  # noqa: S603 - a fixed program and arguments, no shell
            [settings.TESSERACT_CMD, "stdin", "stdout", "-l", "eng", "--psm", "4"],
            input=buffer.getvalue(),
            capture_output=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        logger.error("Reading a PVC with Tesseract failed: %s", type(error).__name__)
        raise ReadFailed from error
    if result.returncode != 0:
        logger.error("Reading a PVC with Tesseract failed: exit code %s", result.returncode)
        raise ReadFailed
    return result.stdout.decode("utf-8", errors="replace")


def _google(image: Image.Image) -> str:
    image = image.convert("RGB")
    image.thumbnail((GOOGLE_MAX_SIDE, GOOGLE_MAX_SIDE))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=90)
    body = {
        "requests": [
            {
                "image": {"content": base64.b64encode(buffer.getvalue()).decode()},
                "features": [{"type": "TEXT_DETECTION"}],
                "imageContext": {"languageHints": ["en"]},
            }
        ]
    }
    request = urllib.request.Request(  # noqa: S310 - a fixed https URL
        GOOGLE_VISION_URL,
        data=json.dumps(body).encode(),
        # In a header, not the URL, so the key stays out of proxy logs.
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": settings.GOOGLE_VISION_API_KEY,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
            reply = json.load(response)
    except (OSError, http.client.HTTPException, ValueError) as error:
        logger.error("Reading a PVC with Google Vision failed: %s", type(error).__name__)
        raise ReadFailed from error
    responses = reply.get("responses") if isinstance(reply, dict) else None
    if not isinstance(responses, list) or not responses or not isinstance(responses[0], dict):
        logger.error("Reading a PVC with Google Vision failed: invalid response")
        raise ReadFailed
    result = responses[0]
    if "error" in result:
        # Provider error details can contain request data; keep them out of logs.
        logger.error("Reading a PVC with Google Vision failed: provider error")
        raise ReadFailed
    annotation = result.get("fullTextAnnotation", {})
    text = annotation.get("text", "") if isinstance(annotation, dict) else None
    if not isinstance(text, str):
        logger.error("Reading a PVC with Google Vision failed: invalid text response")
        raise ReadFailed
    return text
