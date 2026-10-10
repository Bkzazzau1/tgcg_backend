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
apps/membership/   members, agent accreditation, accreditation audit trail
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
| GET, POST | `/api/v1/members/` | List or register members in your scope; filter `status`, `state__code`, `lga__code`; search name, phone, number |
| GET, PATCH | `/api/v1/members/{membership_number}/` | e.g. `TGCG-000001` |
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

## Quality checks

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run python manage.py makemigrations --check --dry-run
```
