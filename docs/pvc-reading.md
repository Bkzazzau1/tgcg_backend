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

The server uses the first one available:

| Engine | Turn on | Quality | Privacy and cost |
|---|---|---|---|
| Google Cloud Vision | Set `GOOGLE_VISION_API_KEY` | Reads card photos well, including the VIN | The photo goes to Google. Google charges per photo above a free monthly allowance; check current pricing. Say in the privacy notice that card photos may be sent to Google to be read. |
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
full path. The server gives each photo 25 seconds.

## Turning on Google Cloud Vision

1. In a Google Cloud project with billing, enable the Cloud Vision API.
2. Create an API key restricted to the Cloud Vision API.
3. Set `GOOGLE_VISION_API_KEY` in the server's environment (never in the code)
   and restart the API.
4. Update the privacy notice.

The key is sent in a request header, not the URL. If Google is unreachable or
refuses a photo, the person is asked to type the details.
