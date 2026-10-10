# TGCG-EMCOP Backend

Django REST API for **The Game Changer Group Election Monitoring & Collation
Programme**. It serves the Flutter client in `../TheGameChangerGroup`.

## Stack

- Python 3.12, Django 5.2 LTS, Django REST Framework
- JWT auth (SimpleJWT) with refresh rotation and logout blacklist
- PostgreSQL (PostGIS image) in shared environments; SQLite for quick local runs
- OpenAPI docs via drf-spectacular
- [uv](https://docs.astral.sh/uv/) for dependencies, pytest, ruff

## Getting started

```bash
uv sync                                   # create .venv and install
cp .env.example .env                      # then set DJANGO_SECRET_KEY
uv run python manage.py migrate           # also seeds zones, states, districts, LGAs, parties
uv run python manage.py load_polling_units  # 8,809 wards + 176,846 INEC polling units (~5s)
uv run python manage.py createsuperuser   # prompts for access ID + full name
uv run python manage.py runserver
```

- API docs: http://127.0.0.1:8000/api/docs/
- Admin: http://127.0.0.1:8000/admin/
- Health: http://127.0.0.1:8000/api/v1/health/

To use PostgreSQL locally: `docker compose up -d db`, then set
`DATABASE_URL=postgres://tgcg:tgcg@localhost:5432/tgcg` in `.env` and migrate.

## Layout

```
config/            settings (base / dev / test / prod), urls, wsgi, asgi
apps/core/         shared base model, enums mirrored from the Flutter client, health
apps/geography/    zones, states, senatorial districts, LGAs, wards, polling units
apps/accounts/     operator accounts: access ID or phone login, role, scope
apps/membership/   member registration, member home, membership card, volunteering,
                   agent accreditation and its audit trail
apps/announcements/ news for members, addressed to an area
apps/tracking/     agent GPS: check-ins, on-duty live location, coverage
apps/field/        incidents, field reports, evidence uploads, incident audit trail
apps/results/      parties, elections, result submissions, integrity checks, collation
```

## API (v1)

| Method | Path | Notes |
|---|---|---|
| GET | `/api/v1/health/` | Public liveness and database check |
| POST | `/api/v1/auth/login/` | `{"username": "<access ID or phone>", "password": ...}` returns `access`, `refresh` and `user` |
| POST | `/api/v1/auth/refresh/` | Rotates the refresh token |
| POST | `/api/v1/auth/logout/` | `{"refresh": ...}` revokes it |
| GET | `/api/v1/auth/me/` | Current user, with `scope` shaped like the client's `GeographicScope` |
| GET | `/api/v1/geography/{zones,states,senatorial-districts,lgas,wards,polling-units}/` | Read-only reference data; filter with e.g. `?state__code=LA` |
| POST | `/api/v1/register/code/` | Public. `{"phone_number", "vin"}` checks both are free and sends an SMS code |
| POST | `/api/v1/register/` | Public, multipart. Self-registration from a scanned PVC; returns a signed-in member session |
| POST | `/api/v1/register/pin-code/`, `/api/v1/register/pin/` | Public. Set or reset a member's PIN by SMS code |
| POST | `/api/v1/members/register/` | Coordinator registers someone in person (multipart, PVC optional) |
| GET | `/api/v1/members/` | Members in your scope; filter `status`, `registration_channel`, `gender`, scope codes; search name, phone, number |
| GET, PATCH | `/api/v1/members/{membership_number}/` | e.g. `TGCG-000001` |
| GET | `/api/v1/members/{membership_number}/photo/` | Member photo (scope-checked) |
| GET | `/api/v1/members/verify/?token=` | Staff scan a membership card QR code |
| GET, PATCH | `/api/v1/me/member/` | Member home: own card and profile (email and occupation are editable) |
| GET | `/api/v1/me/member/photo/`, `/api/v1/me/polling-unit/`, `/api/v1/me/announcements/` | Member home |
| GET, POST | `/api/v1/me/volunteer/` | Apply to serve as a polling-unit agent |
| GET | `/api/v1/volunteers/` | Coordinators' queue; `POST .../{id}/accept/` or `.../{id}/decline/` |
| GET, POST, PATCH, DELETE | `/api/v1/announcements/` | Staff with `sendBroadcast`; addressed to an area inside their scope |
| GET, POST | `/api/v1/agents/` | List or accredit agents (`member`, `role`, `scope`) |
| GET | `/api/v1/agents/{agent_id}/` | e.g. `AG-00001` |
| PATCH | `/api/v1/agents/{agent_id}/readiness/` | `training_completed`, `biometric_enrolled`, `device_id`, `sim_fingerprint` |
| POST | `/api/v1/agents/{agent_id}/status/` | `{"status": "approved" \| "suspended" \| "revoked", "reason": ...}` |
| GET | `/api/v1/agents/{agent_id}/events/` | Accreditation audit trail |
| GET, POST | `/api/v1/incidents/` | Filters: `status`, `severity`, `min_severity`, `category`, `open`, scope codes. Most severe first |
| GET | `/api/v1/incidents/{reference}/` | e.g. `INC-000001`, with evidence |
| POST | `/api/v1/incidents/{reference}/status/` | `{"status": ..., "note": ...}` |
| POST | `/api/v1/incidents/{reference}/assign/` | `{"team": ..., "note": ...}` |
| GET | `/api/v1/incidents/{reference}/events/` | Incident audit trail |
| GET, POST | `/api/v1/incidents/{reference}/evidence/` | Multipart upload: `file`, `type`, `caption`, `latitude`, `longitude`, `captured_at`, `content_hash`, `client_id` |
| GET, POST | `/api/v1/field-reports/` | Optional `incident` reference |
| POST | `/api/v1/field-reports/{reference}/review/` | `{"accepted": bool, "reason": ...}` |
| GET, POST | `/api/v1/field-reports/{reference}/evidence/` | As for incidents |
| GET | `/api/v1/evidence/{id}/download/` | Authenticated, scope-checked file download |
| GET | `/api/v1/parties/`, `/api/v1/elections/` | Ballot reference data |
| GET, POST | `/api/v1/results/` | Submit a polling-unit result (see below); filter `election__code`, `status`, scope codes |
| GET | `/api/v1/results/{reference}/` | e.g. `RES-000001`, with validation and result form |
| POST | `/api/v1/results/{reference}/verify/` | `{"note": ...}` |
| POST | `/api/v1/results/{reference}/dispute/` | `{"reason": ...}` |
| POST | `/api/v1/results/{reference}/reject/` | `{"reason": ...}` |
| GET | `/api/v1/results/{reference}/events/` | Result audit trail |
| GET, POST | `/api/v1/results/{reference}/evidence/` | Upload the signed result sheet (`type=resultForm`) |
| GET, POST | `/api/v1/tracking/check-ins/` | Agent checks in with a GPS fix (required). `POST .../{id}/confirm-location/` lets a coordinator confirm it |
| GET, POST | `/api/v1/tracking/duty/` | `{"on_duty": true, "gps": {...}}` starts sharing live location, and `false` stops it |
| POST | `/api/v1/tracking/pings/` | Upload up to 500 queued on-duty GPS readings |
| GET | `/api/v1/tracking/live/` | On-duty agents and their latest position in an area (`viewSituationRoom`) |
| GET | `/api/v1/tracking/coverage/?level=&code=&date=` | Check-in coverage per area: on site, away, suspect, not checked in |
| GET | `/api/v1/collation/?election=...&level=...&code=...` | Unofficial totals from verified results, with a child breakdown |

Enum values (roles, levels, statuses) use the Flutter client's camelCase names,
so payloads map straight onto `Enum.values.byName(...)` in Dart.

## Geography

Codes match the client exactly (state `KD`, district `SD/031/KD`, LGA
`KD-KADUNA-NORTH`). `apps/geography/data/nigeria_geography.json` is generated
from the Flutter registry:

```bash
# in ../TheGameChangerGroup
dart run tool/export_geography.dart ../tgcg_backend/apps/geography/data/nigeria_geography.json
# then, here
uv run python manage.py load_geography
```

Migration `geography.0002` seeds it automatically on a fresh database.

### Wards and polling units

`load_polling_units` loads all 8,809 wards and 176,846 polling units from
`apps/geography/data/inec_polling_units.csv.gz`. It uses INEC codes: ward
`18-23-01`, polling unit `18-23-01-001`, and each LGA's INEC code (`18-23`) is
stored. The file's source, checksum and the checks it passed (including
agreement with an independent INEC list for Zaria) are in
`apps/geography/data/README.md`.

- Re-running is safe. Names are refreshed, but `registered_voters` and
  coordinates added locally are kept.
- Units dropped from a newer file are reported, not deleted.
- `--lga KD-ZARIA` loads a single LGA.
- The data has **no registered voter counts or coordinates**. Until they are
  added, the "accredited exceeds registered" check uses the figure on the
  submitted result sheet.

## Users and scope

A user signs in with an **access ID** (stored upper-case) or a **Nigerian phone
number** (stored as `+234…`). Each user has a role and a scope level. Set the
most specific unit (for example an LGA) and the parent units (district, state,
zone) are filled in automatically. Coordinator and agent roles must match their
scope level: a state coordinator needs a state, and a polling unit agent needs a
polling unit.

## Permissions

Role capabilities in `apps/core/permissions.py` are ported from the client's
`TgcgPermissionPolicy`; keep the two in sync. `GET /auth/me/` returns the
caller's capabilities. Every list is limited to the caller's scope: a Kaduna
state coordinator only sees Kaduna members and agents.

| Action | Capability |
|---|---|
| View members / agents | `manageMembership`, `accreditAgents` or `manageAgentAssignments` |
| Register or edit members | `manageMembership` |
| Accredit, approve, suspend, revoke | `accreditAgents` |
| Update agent readiness | `accreditAgents` or `manageAgentAssignments` |

## Member registration

**Self-registration in the app.** The PVC is required.

1. The app scans the PVC and reads the name, VIN, gender, date of birth and
   DELIM code. The member confirms the details and takes a selfie.
2. `POST /register/code/` with the phone and VIN. It rejects a phone or PVC
   that is already registered, then sends a 6-digit SMS code.
3. `POST /register/` (multipart) with: `phone_number`, `code`, `vin`,
   `delim`, `full_name`, `gender`, `date_of_birth`, `occupation`, `email`,
   `pin`, `consent=true`, `photo` (the selfie) and `pvc_image`.
   - The DELIM code (e.g. `18/23/01/001`) places the member at that INEC
     polling unit.
   - The member is active immediately and receives a sign-in session.

**Assisted registration.** A coordinator with `registerMembers` (zonal,
state, LGA or ward coordinators) or `manageMembership` uses
`POST /members/register/`.

- The PVC is optional. Without one, the member must be placed at least at
  their LGA.
- The member reads out the SMS code sent to their phone, so a basic phone is
  enough.
- The member can later get the app with `register/pin-code/` and
  `register/pin/`.

**Signing in.** Members sign in at `/auth/login/` with their phone and a
6-digit PIN. Obvious PINs (`123456`, `111111` and so on) are refused. After
5 failed attempts (members and staff alike), the account locks for
15 minutes.

**Rules:**

- **One PVC and one phone per member.** A phone that already belongs to a
  staff account cannot be reused.
- **SMS codes:** 6 digits, expire after 10 minutes, at most 5 tries, 1 per
  minute and 5 per hour per phone. Codes are stored only as a keyed hash.
  Wrong attempts are recorded even though the request fails, because these
  views (and login) run outside the request transaction. Validation happens
  before the code is used up, so a typo does not cost the member their code.
- **Privacy:** selfies and PVC images are private files, never served from
  `/media/`. Staff see only the last 4 digits of a VIN. Recording consent is
  mandatory under the Nigeria Data Protection Act.
- **Membership card:** the QR code holds a signed token, so cards cannot be
  forged or found by guessing numbers. Staff scan it at `/members/verify/`.
- **INEC:** the backend cannot confirm a PVC with INEC (there is no public
  API). It checks the VIN format and that the DELIM is a real polling unit.

**SMS:** set `SMS_BACKEND=termii`, `TERMII_API_KEY` and `TERMII_SENDER_ID`
(the sender ID must be approved by Termii). The default `console` backend
writes codes to the server log, and production refuses to start with it.

## Accreditation rules

- Status flow: `pending → approved | revoked`, `approved → suspended | revoked`,
  `suspended → approved | revoked`. `revoked` is final.
- Approval requires `training_completed` and `biometric_enrolled`.
- Suspending or revoking requires a reason. Every change is recorded in the
  audit trail with the actor.
- A member holds at most one active (pending or approved) accreditation.
- A polling unit has at most one approved polling-unit agent.
- The role must match the scope level, for example a polling-unit agent must
  be assigned to a polling unit.
- Members and agents get sequential numbers (`TGCG-000001`, `AG-00001`).
  Sending a device `client_id` makes offline retries idempotent.

## Incidents and field reports

- New incidents and reports default to the reporter's own scope, and can only
  be filed inside it. A polling-unit agent reports at their polling unit.
- People with `viewIncidents` see every incident in their scope. Others
  (polling-unit agents, observers) see only what they reported.
- Status flow:

  | From | To |
  |---|---|
  | reported | acknowledged, escalated, closed |
  | acknowledged | investigating, escalated, resolved, closed, or assigned via `/assign/` |
  | assigned / investigating / escalated | investigating, escalated, resolved |
  | resolved | closed, or investigating to reopen |
  | closed | final |

- Who can move an incident: acknowledge, investigate or escalate needs
  `acknowledgeIncident`. Assigning needs `assignIncident`. Resolving needs
  `assignIncident` or `closeIncident`. Closing needs `closeIncident`.
- Escalating, resolving, closing and reopening need a note. Every change is
  kept in `/events/` with the actor.
- Coordinates must fall inside Nigeria and must be sent as a pair.
- Field reports are reviewed (verified or rejected) by holders of
  `verifyFieldReport`. Rejecting needs a reason, and nobody can review their
  own report.

## Evidence

- Only the record's reporter, or someone with `manageEvidence` in scope, can
  attach evidence. Nothing can be attached to a closed incident.
- Accepted files: photo (jpg, png, webp, heic), video (mp4, mov, 3gp, webm),
  audio (m4a, aac, mp3, wav, ogg, amr), document (pdf), and result form
  (image or pdf).
- Size limits: 25 MB, or 150 MB for video (`EVIDENCE_MAX_UPLOAD_MB`,
  `EVIDENCE_MAX_VIDEO_MB`).
- The server checks the file signature, computes SHA-256, and rejects the
  upload if it differs from the `content_hash` recorded on the device.
- Files are stored under random names and never served from `/media/`.
  Downloads go through `/evidence/{id}/download/`, which checks scope and
  `viewEvidence`. In production, use private object storage.

## Results and collation

TGCG collation is **unofficial**: every result and collation response carries
`"official": false`, and it never replaces INEC declarations.

- **Elections:** created in the admin with a type, an area (presidential:
  country, governorship: state, senatorial: district) and the parties on the
  ballot. Results are accepted only while an election is `open`.
- **Parties:** the 18 parties registered by INEC for 2023 are seeded. Check
  them against INEC's current register before each election.
- **Submitting:** send `election`, `polling_unit`, `party_votes`
  (`{"APC": 120, ...}`), `total_valid_votes`, `accredited_voters` and
  optionally `rejected_votes`, `registered_voters`, `ocr_party_votes`,
  `ocr_confidence`, `client_id`. The polling unit must be inside both the
  election and the submitter's scope. Parties left out count as 0, and
  parties not on the ballot are rejected.
- **Integrity checks** (ported from the client's `ResultIntegrityPolicy`):
  - the party votes add up to the total;
  - valid plus rejected votes do not exceed accredited voters;
  - accredited voters do not exceed registered voters, using master data
    when it is known;
  - there is no other live submission for the same polling unit;
  - the submitter is that polling unit's own agent;
  - the OCR figures match the typed figures.

  Any failure sends the result to `underReview`.
- **Review:**
  - Verifying needs `verifyElectionResult` and cannot be done on your own
    submission or on a result that fails arithmetic.
  - Each polling unit has only one verified result per election.
  - Disputing (`disputeElectionResult`) and rejecting both need a reason.
  - Submissions are never edited: correct a result by resubmitting it.
- **Collation:** computed live from verified results only.
  - Defaults to the caller's own scope; `level` and `code` narrow it.
  - Returns expected, reported, verified, under-review, disputed and missing
    polling units, completion, party totals and the leading party.
  - Includes a breakdown by the next level down, limited to the election's
    area.
  - Expected polling units come from master data, so load wards and polling
    units to get completion figures.

## Agent GPS

The field app sends a GPS fix with every check-in, incident, result and
piece of evidence: `{"latitude", "longitude", "accuracy_m", "captured_at",
"is_mocked"}`. The server judges each fix against the agent's polling unit
and gives it one of these statuses:

| Status | Meaning |
|---|---|
| `onSite` | Within `PU_PROXIMITY_METERS` (300 m) of the unit, after allowing for the GPS accuracy |
| `away` | Further than that |
| `unverified` | The unit's location is not known yet, or the fix is less accurate than `GPS_MAX_ACCURACY_M` (150 m) |
| `suspect` | Android reported a mock-location app |
| `noGps` | No fix |

- **Check-in and app results require GPS.** Results sent by SMS, USSD or
  manual entry cannot carry GPS and are not flagged for it. Incidents never
  require GPS, so someone in danger is never blocked, but they show `noGps`.
- **Learning polling unit locations.** INEC publishes no coordinates. When a
  ward, LGA, state or zonal coordinator confirms a check-in
  (`confirmAgentLocation`), a unit with no location takes that check-in's
  position. The fix must not be suspect and must be within 150 m accuracy.
  Later check-ins and results are measured against it. You cannot confirm
  your own check-in.
- **Results:** an app result that is `away` or `suspect` goes to
  `underReview`, with a note giving the distance.
- **Live location** is shared only between "on duty" and "off duty" in the
  app (agents and observers), and readings outside a duty session are
  dropped. Pings are deleted after `LOCATION_PING_RETENTION_DAYS` (30). Run
  `uv run python manage.py purge_location_pings` daily.

## Quality checks

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run python manage.py makemigrations --check --dry-run
```
