# Joining TGCG by USSD

People without a smartphone or data can join TGCG from any phone by dialling
a USSD code. The code lives in `apps/membership/ussd.py` (the menu),
`ussd_views.py` (the provider's callback) and `registration.ussd_register`.
It is off until a callback token is configured.

## What the member sees

```
Welcome to TGCG
1. Join TGCG
2. My membership
```

Joining asks, one screen at a time:

1. Full name, as written on the PVC (at least two names).
2. The polling unit code on the PVC, marked CODE (for example 18-23-01-001;
   older cards mark it DELIM, while today's DELIM line names the state, LGA
   and ward). Members can type it one number at a time, and each answer is
   named back to them ("Kaduna State. Enter the 2nd number..."), or all at
   once with `*` between the numbers: `18*23*01*001`.
3. The polling unit found, to confirm or enter again.
4. Gender.
5. Date of birth as DDMMYYYY, or 0 to skip.
6. The VIN on the PVC, or 0 to skip. Typing 19 letters on a keypad is slow,
   so it is optional here.
7. A 6-digit PIN, twice. With their phone number, it signs them in to the
   TGCG app later.
8. The data-use notice: "1. I agree, join TGCG" or "2. Cancel".

The last screen gives the membership number. "My membership" shows the name,
membership number, polling unit and status for the phone dialling: "active
member" (with "ID checked" once verified), "suspended" or "ended".

A wrong answer is asked again with a short reason. A phone number or VIN that
is already registered, or a staff account's number, cannot join again.

## What the server records

- Registration channel `ussd`, with consent and the PIN-protected sign-in
  account created as for app registrations.
- **Phone verified**, because the mobile network supplied the number. This
  depends on the callback being genuine, which is why the token below must
  stay secret.
- **No selfie and no photo of the card.** The selfie check fails and the PVC
  check is pending (VIN given) or failed (VIN skipped). That does not limit
  their membership, which needs no identity check. Only to work as an agent
  or hold a role must they add a selfie and be verified; letting them add one
  is in TheGameChangerGroup's `docs/identity-checks-plan.md`.

The session's text holds the member's PIN, so requests are never logged. If a
request fails, only the error type and code location are logged.

## Turning it on

1. **Get a USSD code** from a licensed aggregator, for example a shared code
   such as `*384*1234#`. A dedicated short code is costlier and slower to get.
   The callback must use Africa's Talking's format, which most Nigerian
   aggregators share: a form POST of `sessionId`, `serviceCode`,
   `phoneNumber` and `text` (all answers so far, joined by `*`), answered in
   plain text starting `CON ` (more to come) or `END ` (session over). A
   provider with another format needs a small adapter in `ussd_views.py`.
2. **Set the server's environment** and restart the API:

   ```bash
   USSD_CALLBACK_TOKEN=<at least 32 random characters>
   USSD_SERVICE_CODE=*384*1234#
   ```

   Generate the token with
   `uv run python -c "import secrets; print(secrets.token_urlsafe(32))"`.
   Production refuses a shorter one. Requests for any other service code are
   refused.
3. **Give the provider the callback URL**:
   `https://api.tgcg.online/api/v1/ussd/<USSD_CALLBACK_TOKEN>/`.
4. **Allow only the provider's addresses** to that path in Nginx, using the
   list the provider publishes. The token appears in the URL, so keep the
   access log for that location off or root-only, and change the token if it
   leaks.
5. **Try it** from a real phone on each network (MTN, Airtel, Glo, 9mobile).
   Networks end a session that waits too long for an answer, so check that
   members can type their names in time.

To try it locally:

```bash
curl -d "sessionId=s1&serviceCode=*384*1234%23&phoneNumber=%2B2348031234567&text=" \
  http://127.0.0.1:8000/api/v1/ussd/<token>/
curl -d "sessionId=s1&serviceCode=*384*1234%23&phoneNumber=%2B2348031234567&text=1" \
  http://127.0.0.1:8000/api/v1/ussd/<token>/
```

## Limits

- A screen holds at most 160 characters. Long names of polling units, wards
  and members are shortened on screen, never in the records.
- If a session times out, the member starts again. The answers are not kept
  between sessions.
- No welcome SMS is sent yet; the last screen shows the membership number.
