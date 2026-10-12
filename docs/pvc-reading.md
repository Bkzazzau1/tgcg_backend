# Reading PVC photos from browsers

When someone registers in the Android or iPhone app, the phone reads the PVC
itself (Google ML Kit, on the device). A browser cannot, so the web app sends
the card photo to `POST /api/v1/register/read-pvc/` and gets back the text on
the card. The app then picks out the name, VIN, polling unit code, date of
birth, gender and occupation, fills in the form, and asks the person to check
each one. Reading a card is not validating it.

Nothing is stored, and neither the photo nor the text is logged. The endpoint
needs no sign-in, so it is limited to 30 photos an hour from one address
(`API_THROTTLE_PVC_READING`) and to images of up to `MEMBER_PHOTO_MAX_MB`.

## Engines

Both readers can be enabled. The server tries Google Cloud Vision first when
its key is configured. If that request fails, times out or returns only blank
text, it tries Tesseract when installed. Without a Google key it uses
Tesseract directly; without either reader the person types the details.

| Engine | Turn on | Quality | Privacy and cost |
|---|---|---|---|
| Google Cloud Vision | Set `GOOGLE_VISION_API_KEY` | Reads card photos well, including the VIN | The photo goes to Google. Google charges per photo above a free monthly allowance; check current pricing. With a key configured, the first attempt sends the photo to Google, including requests that later use Tesseract. |
| Tesseract | Install the `tesseract` program | Usually gets the name, date of birth and gender; often misses or misreads the VIN and polling unit code, printed small on the card's patterned background | Runs on this server; free |
| None | | The person types the details, as before | |

Tried on a real card photo (2026-10-12), Tesseract read the name, date of
birth and gender, misread one digit of the polling unit code, and could not
read the VIN. A wrong code is caught when the polling unit is looked up, and
people check every field before continuing, but Google Cloud Vision is the
better choice where its privacy and cost are acceptable.

## Turning on Tesseract (AlmaLinux 9)

```bash
dnf install -y epel-release
dnf install -y tesseract
tesseract --list-langs    # must list "eng"; if not: dnf install -y tesseract-langpack-eng
systemctl restart tgcg-api.service
```

If `tesseract` is not on the API service's `PATH`, set `TESSERACT_CMD` to its
full path. Each reader has a 25-second timeout. A Google attempt followed by
Tesseract can take about 50 seconds plus image processing and transfer, so
client and proxy timeouts must allow the sequential attempts.

## Turning on Google Cloud Vision

1. In a Google Cloud project with billing, enable the Cloud Vision API.
2. Create an API key restricted to the Cloud Vision API.
3. Set `GOOGLE_VISION_API_KEY` in the server's environment (never in the code)
   and restart the API.
4. Install Tesseract as well to enable the local fallback.

The key is sent in a request header, not the URL. Google errors, quota
refusals, malformed replies and blank text trigger Tesseract when available.
The local attempt uses the original upright image, preserving detail that
Google's smaller upload does not retain. If both attempts fail, or Google
fails without Tesseract installed, the API returns 503 with guidance to type
the details.

A non-empty Google reading is returned without a second read. The server
returns one reader's text and never combines conflicting readings or checks
that every field was recognized. Members still check all details against
their card. OCR uploads and extracted text are not stored or logged by the
TGCG reader; errors log only their type, exit code or a fixed description.
