# Joining TGCG by USSD

People without a smartphone or data can join TGCG from any phone by dialling
a USSD code. The menu is in `apps/membership/ussd.py`, the provider callback
is in `ussd_views.py`, and membership creation is in `registration.py`.
The callback stays off until its private token is configured. This guide
describes quick membership and resumable progress; the deployment notes
record which version is live.

## What the member sees

```
Welcome to TGCG
1. Join / continue
2. My membership
```

Joining asks for only the PVC details needed to create membership:

1. **VIN:** the required 19 letters and digits on the PVC. It cannot be
   skipped.
2. **Polling unit code:** the full code marked CODE on the card, such as
   `18-23-01-001`, or `18*23*01*001`. A caller can also enter its state,
   LGA, ward and polling unit numbers on separate screens. Older cards may
   mark the numeric code DELIM; today's DELIM line usually names the places.
3. **Confirm the unit and agree to data use on the same screen:** `1` joins
   TGCG, `2` changes the polling unit, and `0` cancels.

Successful confirmation returns the membership number. The caller is an
active member awaiting review, with identity unverified. No name, gender,
date of birth or app PIN is needed to finish these joining steps.

"My membership" shows the membership number, polling unit and membership
status for the phone dialling, with the name once it has been added. An
existing membership is shown rather than created again. Conflicting VIN
or phone claims are rejected with a short explanation.

## Adding a name and app PIN later

An existing member can dial again and choose menu `1` to complete their
profile. They enter their full name as written on the PVC, then a 6-digit
app PIN and the same PIN again. They can leave this for another session;
it does not cancel membership.

Quick membership initially has an empty name and no sign-in account
(`Member.user` is null). It does not enable app sign-in. After the optional
name and repeated PIN have been saved, the caller can use their phone
number and PIN to sign in to the member app. This does not verify their
identity or grant permission to work as an agent.

## Continuing after a session ends

The progress notice explains that unfinished steps are kept for 30 days
against the normalized phone number. Production PostgreSQL stores this
draft, so callers can dial again and use menu `1` to continue even when
the provider gives them another session ID or the API restarts or uses
another worker.

Native USSD session timeouts are controlled by the provider and mobile
network. Saving progress does not extend their session timeout. Cancelling
clears the unfinished draft; it leaves any existing membership intact.
An in-flight session using the old registration format is asked to redial
so it can use the new menu.

Run the following regularly to remove expired unfinished progress:

```bash
python manage.py purge_ussd_progress
```

This removes expired progress, not memberships. Apply the resumable-progress
database migration before activating the new application code.

## What the server records

- Registration channel `ussd`, the network-supplied phone number, required
  VIN, polling unit and the combined confirmation/data-use consent.
- Active membership with status `pendingReview`, verification status
  `unverified`, an empty full name and no `User` until optional profile
  completion.
- Phone verification depends on a genuine provider callback, protected by
  the private callback token and provider IP restrictions.
- No selfie or card photo. Reading or typing a VIN is not an identity check.
  Coordinators still review the member before operational roles can be used.

Duplicate requests and previously processed callback prefixes are matched
using keyed HMAC digests. Raw cumulative callback text and raw PINs are not
stored for replay matching. PINs are not retained in plain text. Retries
must not create a second membership or sign-in account.

The provider's cumulative `text` can contain a PIN during optional profile
completion. Do not log callback bodies or secret-bearing callback URLs.
Failure diagnostics must omit the supplied text and credentials.

## Turning it on

The callback token and service code used by an already enabled channel do
not change for this flow update. No Africa's Talking API key is needed.

1. **Get a USSD code** from a licensed aggregator, for example a shared code
   such as `*384*1234#`. The callback accepts Africa's Talking's form POST
   fields `sessionId`, `serviceCode`, `phoneNumber` and `text` (all answers
   so far joined by `*`). Replies are `text/plain` starting `CON ` for more
   input or `END ` to end the session. Another provider format needs an
   adapter in `ussd_views.py`.
2. **Configure the server environment** for a new channel and restart the API:

   ```bash
   USSD_CALLBACK_TOKEN=<at least 32 random characters>
   USSD_SERVICE_CODE=*384*1234#
   ```

   Generate the token privately with
   `uv run python -c "import secrets; print(secrets.token_urlsafe(32))"`.
   Production refuses a shorter token, and other service codes are refused.
3. **Give the provider the complete private callback URL.** Its path is
   `/api/v1/ussd/<USSD_CALLBACK_TOKEN>/` on the HTTPS API host. Replace the
   placeholder with the existing configured token; never publish that URL.
4. **Allow only the provider's IP/CIDR ranges** for that path in Nginx.
   Keep access logging off for secret-bearing callback URLs and rotate the
   token if it leaks.
5. **Test the flow** in the provider's simulator and on real phones on MTN,
   Airtel, Glo and 9mobile. Check joining, optional profile completion,
   redialling to continue, cancellation and duplicate callbacks.

To try the initial menu and VIN prompt locally with synthetic details:

```bash
curl -d "sessionId=s1&serviceCode=*384*1234%23&phoneNumber=%2B2348031234567&text=" \
  http://127.0.0.1:8000/api/v1/ussd/<token>/
curl -d "sessionId=s1&serviceCode=*384*1234%23&phoneNumber=%2B2348031234567&text=1" \
  http://127.0.0.1:8000/api/v1/ussd/<token>/
```

## Limits

- Screens fit within 160 characters. Long display names are shortened
  on screen without changing stored values.
- Progress is kept for 30 days, but session duration is still provider-controlled.
- No welcome SMS is sent yet; the last joining screen shows the membership number.
