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
uv run python manage.py load_inec_locations # unit coordinates from INEC's locator (collected LGAs)
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
apps/wallets/      members' wallets: payments with a second approver, payout accounts
apps/projects/     scheduled field work (elections, assignments): who takes part, where
apps/messaging/    bulk SMS, WhatsApp and app notifications; saved lists; the inbox
```

## API (v1)

| Method | Path | Notes |
|---|---|---|
| GET | `/api/v1/health/` | Public liveness and database check |
| POST | `/api/v1/auth/login/` | `{"username": "<access ID or phone>", "password": ...}` returns `access`, `refresh` and `user` |
| POST | `/api/v1/auth/refresh/` | Rotates the refresh token |
| POST | `/api/v1/auth/logout/` | `{"refresh": ...}` revokes it |
| POST | `/api/v1/auth/password/` | `{"current_password", "new_password"}` changes one's own PIN (members) or password (staff), ends every other session and returns a new `access` and `refresh` |
| GET | `/api/v1/auth/me/` | Current user, with `scope` shaped like the client's `GeographicScope` |
| GET | `/api/v1/geography/{zones,states,senatorial-districts,lgas,wards,polling-units}/` | Read-only reference data; filter with e.g. `?state__code=LA` |
| GET | `/api/v1/register/options/` | Public. `{"phone_code_required"}`: whether joining in the app needs an SMS code |
| GET | `/api/v1/register/places/{states,lgas,wards,polling-units}/` | Public. Lists to find a polling unit by name (`?state=`, `?lga=`, `?ward=`): INEC names and codes, no map positions |
| POST | `/api/v1/register/code/` | Public. `{"phone_number", "vin"}` checks both are free and sends an SMS code |
| POST | `/api/v1/register/` | Public, multipart. Self-registration (phone, VIN, polling unit code, name, PIN; optionally `disabilities` as JSON text and `disability_other`); returns a signed-in member session |
| POST | `/api/v1/register/pin-code/`, `/api/v1/register/pin/` | Public. Set or reset a member's PIN by SMS code |
| POST | `/api/v1/members/register/` | Coordinator registers someone in person (multipart, PVC optional) |
| GET | `/api/v1/members/` | Members in your scope; filter `status`, `registration_channel`, `gender`, scope codes, `role` (holding that post now) and `show` (`fullyVerified`, `notFullyVerified`, `neverSignedIn`, `withRole`, `withoutRole`). `q` searches every word in names, membership numbers, the places they registered and their current posts (position and place: `women leader zaria`), or takes a phone number where you may see personal details. Each has `roles` (each post with `area_level` and `area_name`), `gaps` (what the profile lacks), `signed_in`, `last_sign_in` and `verified_how` |
| GET | `/api/v1/members/counts/?level=&code=&by=` | Members registered in each smaller area (`by`, e.g. `ward`) of an area wholly within your reach: `{"total", "counts": {code: n}}` |
| GET | `/api/v1/members/analysis/?level=&code=&by=` | Members and exco in an area wholly within your reach, with each smaller area; `by=geopoliticalZone` lists Nigeria's zones instead of its states (see **Members and exco**) |
| GET | `/api/v1/members/exco/?level=&code=&at=&role=&show=&search=` | Exco posts in that area from the top down, with who holds each; `at` keeps only posts at some levels (`state,senatorialDistrict` for a state's own exco) |
| GET | `/api/v1/members/list-pdf/?level=&code=` | Everyone in that area as one printable list (PDF, A4 landscape): grouped by state, LGA and ward, exco first under their highest post there, then members by polling unit. Each row has the full name, membership number, polling unit and posts, never a phone number. Each download is recorded (`members.list_downloaded`) |
| GET, PATCH | `/api/v1/members/{membership_number}/` | e.g. `TGCG-000001` |
| GET | `/api/v1/members/{membership_number}/photo/` | Member photo (scope-checked) |
| GET | `/api/v1/members/verify/?token=` | Staff scan a membership card QR code |
| GET, PATCH | `/api/v1/me/member/` | Member home: own card and profile. Email and occupation are editable; gender and date of birth can be added, and changed until the identity is checked (audited by field name, identity checks rerun). `disabilities` (any of physical, visual, hearing, speech, intellectual, psychosocial, albinism, other) with `disability_other` are the member's to give, change or take back. `card_missing` lists what the card download still needs |
| GET | `/api/v1/me/member/card/` | The membership card as a PDF: front and back on two ID-card-size pages (85.6 x 54 mm). Needs an active membership, the member's photo, a PVC photo, gender and date of birth, not an identity check; otherwise 409 with `missing`. Roles print once the identity is checked |
| POST | `/api/v1/me/member/photo/` (`photo`) | Multipart. The member adds the selfie left out when joining; replacing it is refused once their identity is checked (a coordinator does it). Audited, and the identity checks rerun |
| GET, POST | `/api/v1/me/member/pvc-scan/` | The member's one PVC scan. GET: `{"can_scan", "latest"}`. POST (multipart: `pvc_image`, `full_name`, `vin`, `delim`, optional `gender`, `date_of_birth`, `occupation`): the card's details replace the typed ones, at once, or after a coordinator's second check when the identity is checked. Refused once a card is on record (from joining or an earlier scan) or while one waits; `.../<id>/withdraw/` takes a waiting scan back |
| GET, POST | `/api/v1/me/registered/` | Anyone signed in registers a member with `full_name`, `phone_number` and `consent=true` (optional `scope`), with no limit; the phone number is the first password. GET lists the members they registered, newest first (`?search=`), with `missing`, `can_complete` and `signed_in` |
| GET, PATCH | `/api/v1/me/registered/<number>/` (also `photo/`, `pvc-scan/`) | The person who registered a member completes the registration until the identity is checked: the fields of `/me/member/`, the photo and the one PVC scan, plus `full_name` and `scope` until the PVC is on record and `phone_number` until the member first signs in. Every change is audited against them |
| GET, POST | `/api/v1/pvc-updates/` | `verifyMemberIdentity` for the member's area (and the unit on the card): scans awaiting a second check, with what they change; `<id>/card/`, `<id>/approve/`, `<id>/reject/` (reason required). Approving keeps the identity checked, by the approver |
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
| GET, POST | `/api/v1/incidents/{reference}/security/` | The security officers it was sent to, each step with its time (`can_send`, `dispatches`). POST `{"officer": access ID, "note"}` sends it to one (the Situation Room only) |
| GET | `/api/v1/incidents/{reference}/security/nearest/` | The Situation Room's pick list: officers nearest first, by live location or the area of their role (`?search=`, `?limit=` up to 50) |
| POST | `/api/v1/security/dispatches/{id}/cancel/` | `{"reason"}`: the Situation Room takes it back; the officer is told |
| GET | `/api/v1/me/security/` | A security officer's incidents: those still with them and those finished in the last week, with the place, a point to navigate to and the reporter's phone |
| POST | `/api/v1/me/security/{id}/respond/` | `{"step": "accepted" \| "declined" \| "onTheWay" \| "arrived" \| "resolved", "note"}`; declining and resolving need a note |
| POST | `/api/v1/incidents/{reference}/live/start/`, `watch/`, `end/` | Live video of an incident: a LiveKit pass to broadcast (its reporter) or watch (whoever sees it); `end` for the broadcaster or a coordinator over it |
| POST | `/api/v1/livekit/webhook/` | Events LiveKit posts, signed with the API key: rooms closing, recordings ending |
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
| GET, POST | `/api/v1/tracking/check-ins/` | An agent or polling unit executive (`fieldDuty`) checks in at their unit with a GPS fix (required). `POST .../{id}/confirm-location/` lets a coordinator confirm it |
| GET, POST | `/api/v1/tracking/duty/` | `{"on_duty": true, "gps": {...}}` starts sharing live location, only during a project the account takes part in (the app does it without a switch); `false` stops it |
| GET, POST, PATCH | `/api/v1/projects/` | Projects in your areas, for everyone with a role (all for an administrator). POST/PATCH `{"name", "description", "starts_at", "ends_at", "areas", "election", "check_place"}` and `.../{id}/cancel/` (reason required) need `manageProjects` |
| GET, POST | `/api/v1/projects/{id}/participants/` | GET with `viewLiveOperations` or `assignProjectParticipants`: the people in your areas. POST `{"member", "place"}` with `assignProjectParticipants` over the place (by default the person's own polling unit) |
| GET | `/api/v1/projects/{id}/monitor/` | Each person's state in your areas (`atPlace`, `away`, `notSharing`, `notStarted`, `invited`...) and the project's incidents, reports and results there |
| POST | `/api/v1/project-participants/{id}/remove/` | Take someone out of a project (reason required) |
| GET | `/api/v1/me/projects/` | The member's projects with `my_participation`; `POST /api/v1/me/projects/{participation}/accept/`, `decline/` or `withdraw/` |
| GET, POST | `/api/v1/broadcasts/` | Bulk messages. POST `{"channels": ["sms", "whatsapp", "push"], "title", "body", "groups": [...]}` sends at once; each channel needs `sendSms`, `sendWhatsApp` or `sendPushNotifications` and reaches only the sender's areas. GET: what you sent (an administrator, everything), with `delivered` counts |
| POST | `/api/v1/broadcasts/preview/` | The same body: how many people each channel reaches, and how many SMS pages the text takes |
| GET, POST, PATCH, DELETE | `/api/v1/message-lists/` | Your saved lists. POST `{"name", "members": [membership numbers], "groups": [...]}`; PATCH `{"name", "add", "remove"}` |
| POST, DELETE | `/api/v1/me/devices/` | `{"token", "platform"}`: this phone receives the account's app notifications, or stops |
| GET | `/api/v1/me/messages/` | The member's inbox: messages sent to them on any channel |
| POST | `/api/v1/tracking/pings/` | Upload up to 500 queued on-duty GPS readings |
| GET | `/api/v1/tracking/live/` | On-duty agents and their latest position in an area (`viewLiveOperations`) |
| GET | `/api/v1/tracking/coverage/?level=&code=&date=` | Check-in coverage per area: on site, away, suspect, not checked in |
| GET | `/api/v1/mapping/units/` | Polling units in your area with `mapping_status`; filter `mapping_status`, `ward__code`; search |
| POST | `/api/v1/mapping/units/{code}/pin/` | Multipart: `gps` (JSON), `photo`, `at_entrance`, `name_matches`, `note` |
| POST | `/api/v1/mapping/units/{code}/problem/` | `{"problem": "notFound" \| "moved" \| "nameMismatch" \| "other", "note": ...}` |
| GET | `/api/v1/mapping/units/{code}/pins/` | Pin history for a unit |
| POST | `/api/v1/mapping/pins/{id}/accept/` | Resolve a conflict with this pin |
| GET | `/api/v1/mapping/progress/?level=&code=` | Mapped, confirmed, conflict and problem counts, with a child breakdown |
| GET | `/api/v1/collation/?election=...&level=...&code=...` | Unofficial totals from verified results, with a child breakdown |
| GET | `/api/v1/me/wallet/` | Member home: the wallet (what TGCG owes, its history, the payout account). `has_wallet` only for a verified member; `account_name` is always the member's name |
| PUT | `/api/v1/me/wallet/bank-account/` | `{"bank", "account_number"}` (bank from `/wallets/banks/`, 10 digits). The account name cannot be sent |
| GET | `/api/v1/wallets/banks/` | Banks a wallet can be paid into |
| GET | `/api/v1/wallets/{membership_number}/` | `viewWallets`: a member's wallet in your area, the account number masked |
| GET, POST | `/api/v1/wallet-payments/` | Payments in your area (`viewWallets`, `creditWallets` or `approveWalletCredits`). POST `{"member", "amount" (naira), "purpose"}` with `creditWallets`; `.../{id}/approve/` or `reject/` (reason required) by another person with `approveWalletCredits`; `cancel/` by the requester |

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
- The data has **no registered voter counts**. Until they are added, the
  "accredited exceeds registered" check uses the figure on the submitted
  result sheet.

### Polling unit locations

INEC's public [Polling Unit Locator](https://cvr.inecnigeria.org/pu) gives
each unit's coordinates, one unit at a time. They are collected, checked and
imported in three steps:

```bash
python scripts/inec_locator.py --out inec_locator.csv --state 18     # Kaduna; omit for all states
uv run --with shapely python scripts/check_inec_locations.py \
    --locations inec_locator.csv --geo ../TheGameChangerGroup/assets/geo
uv run python manage.py load_inec_locations
```

- The collector sends one request per second and can be stopped at any time;
  re-running it continues where it stopped. The whole country is about
  186,000 requests, roughly two days.
- The check rejects points outside their unit's LGA and spots shared by units
  of several wards, and writes `apps/geography/data/inec_locations.csv.gz`.
- An INEC location counts as a first pin: members and agents get directions
  straight away, and one coordinator pin nearby confirms it. Locations from the
  field are never overwritten.
- Which LGAs are covered so far, and how they scored, is in
  `apps/geography/data/README.md`.

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

## Members and exco

What the National Administrator's Members & Exco page and the dashboard
show (17 October 2026), for anyone who may find members (`MEMBER_READERS`),
and only for an area wholly within their reach.

- **Exco** are the members holding an appointment, as against ordinary
  members (the user's definition, 18 October 2026): national, zonal, state,
  district, LGA, ward and polling unit officers, coordinators and any other
  role. Each level has its own exco: the posts held over that area itself.
  An appointment counts while active or suspended and not ended; the
  protected National Administrator role is not exco.
- **Fully verified** means a verified identity and a profile resting on the
  card: VIN, card photo, selfie, gender and date of birth. Executives from a
  chapter's list are verified on the chapter's word (`verified_how` is
  `chapter`), so many are verified but not fully verified; `gaps` says what
  is missing.
- `analysis/` gives, for the area: members (total, new in 7 and 30 days,
  fully verified, with a post, by verification, standing, gender, age band,
  how they joined, what profiles hold, and new members in each of the last
  12 weeks); exco (people and posts, posts at the area itself, suspended
  posts, by level and by position, and the holders' profiles); and each
  smaller area (states or zones for Nigeria, states for a zone, LGAs for a
  state, wards for an LGA, units for a ward), every one listed even with no
  members, with its members and exco. `breakdowns` names the levels the area
  can be broken down into.
- `exco/` lists posts from the top down (state, district, LGA, ward, unit),
  each with its position, area and holder. The holder's phone is shown only
  where the caller may see personal details.

## Member registration

**Self-registration in the app.** As on USSD, joining needs the phone, the
PVC's VIN and polling unit code, plus a name and PIN for the app account.

1. `GET /register/options/` says whether the phone needs an SMS code. It
   does while `REGISTRATION_PHONE_CODE_REQUIRED` is on, which by default
   means whenever SMS can be sent.
2. If it does, `POST /register/code/` with the phone and VIN. It rejects a
   phone or PVC that is already registered, then sends a 6-digit SMS code.
3. `POST /register/` (multipart) with: `phone_number`, `vin`, `delim`,
   `full_name`, `pin`, `consent=true`, and `code` when one is needed.
   Optional: `gender`, `date_of_birth`, `occupation`, `email`, `photo` (a
   selfie) and `pvc_image` (the app can also read the card to fill the form).
   - The DELIM code (e.g. `18/23/01/001`) places the member at that INEC
     polling unit. Someone who cannot read it sends `polling_unit` instead:
     the code of a unit found by name through `register/places/` (state,
     LGA, ward, then unit). That is recorded (`polling_unit_from_list`) and
     the PVC check stays pending, since it may not be where INEC registered
     them; an approved polling unit change from an updated card clears it.
   - The member is active immediately and receives a sign-in session.
   - A code that is given is always checked. Without one, the phone stays
     unconfirmed (no `phone_verified_at`; its identity check stays pending)
     until an SMS code confirms it, for example on a PIN reset.

**Assisted registration.** A coordinator with `registerMembers` (zonal,
state, LGA or ward coordinators) or `manageMembership` uses
`POST /members/register/`.

- The PVC is optional. Without one, the member must be placed at least at
  their LGA.
- The member reads out the SMS code sent to their phone, so a basic phone is
  enough.
- The member can later get the app with `register/pin-code/` and
  `register/pin/`.

**Registered by another person** (decided on 17 October 2026). Anyone
signed in, ordinary members included, registers members with
`POST /me/registered/`, as many as they wish. Only these are needed:
`full_name`, `phone_number` and `consent=true`, meaning the person agreed to
join TGCG and to TGCG keeping their name and number.

- **Place:** `scope` is optional. Without it the member has no place
  (Nigeria) until their PVC scan places them, so area lists and coordinators
  do not see them until then.
- **Sign-in:** the member signs in with their phone number as the password:
  its 11 digits, as for chapter imports (08031234567). It stays the password
  until they change it with Change PIN. The number is not confirmed by SMS.
- **Review:** with nothing to check yet, the member is not verified and
  waits in no review queue. The usual checks run once a photo, the PVC or
  their gender and date of birth are added.
- **Completing it:** the member completes their registration in the member
  app. So may the person who registered them, at `/me/registered/<number>/`,
  until the identity is checked:
  - the member's own details, as at `/me/member/`;
  - a photo of them;
  - the one PVC scan;
  - corrections to the name and the place, until the PVC is on record;
  - corrections to the phone number, until the member first signs in. The
    first password follows the number.
- **Audit:** every change is recorded against the person who made it.
- **Their list:** `GET /me/registered/` lists only the members this person
  registered, newest first, with `?search=` by name, number or membership
  number.

**Imported from a chapter's list.** A National Administrator imports a
chapter's polling unit executives from a CSV file with the columns `sn`,
`name`, `rank`, `phone` and `polling_unit` (the unit's number within the
ward):

```bash
uv run python manage.py import_executives list.csv --ward 18-23-05 \
    --by <NA access ID> --source "TGCG Zaria LGA chapter: polling unit executives list"
```

- It is a dry run until `--commit` is added. The report names held rows by
  line and S/N only, never by name or number.
- A row is held when its rank is not a polling unit rank, its number is not
  a valid Nigerian mobile number or is shared with another row, its unit is
  not in the ward, or its number is registered already. Correct the file
  and run it again: rows already imported are then held as registered.
- Each person becomes a member at their polling unit, appointed to the role
  for their rank. The six polling unit roles (PU Coordinator, Assistant PU
  Coordinator, PU Youth Leader, Assistant PU Youth Leader, PU Women
  Coordinator, PU Organising Officer) report to the Ward Coordinator and are
  created when missing. They work as their unit's agents (`fieldDuty`: check
  in, share location on duty), join meetings, report incidents and field
  reports, submit results, see their unit's incidents, check identities and
  take part in discussions at their unit. Administrators change this in Role
  Management. The roles imported on 15 October 2026 had the Situation Room,
  which is national, and lacked `fieldDuty`. Correct them with:

  ```bash
  uv run python manage.py remove_role_permission viewSituationRoom --by <NA access ID> \
      --role "PU Coordinator" --role "Assistant PU Coordinator" \
      --role "PU Youth Leader" --role "Assistant PU Youth Leader" \
      --role "PU Women Coordinator" --role "PU Organising Officer"
  uv run python manage.py add_role_permission fieldDuty --by <NA access ID> \
      --role "PU Coordinator" ...  # the same six roles
  ```

  Both are dry runs until `--commit` is added, change custom roles only, and
  write each change to the audit log as the administrator's.
- The first password is the phone number as written (`080...`). Members
  change it to a PIN at `/auth/password/`.
- The chapter checked these people and holds their consent, so the import
  records them as verified by the administrator (channel `import`) on the
  chapter's word. The phone stays unconfirmed until an SMS code confirms it.
- Everything is saved in one transaction. Keep chapter lists out of the
  repository and delete the file once imported.

**A state chapter's officers.** `import_state_officers` takes a CSV file
prepared from the chapter's list, with the columns `sn`, `name`, `role`,
`phone`, `place`, `hold` and, optionally, `area` and `vin`. One file can hold
a state's officers and those of its LGAs, wards and polling units:

```bash
uv run python manage.py import_state_officers officers.csv --state KE \
    --by <NA access ID> --source "TGCG Kebbi State chapter: state officers list"
```

- `role` names the position, e.g. "State Treasurer". "State Coordinator",
  "LGA Coordinator" and "Ward Coordinator" are the system roles. "Deputy
  State Coordinator", created when missing, carries the State Coordinator's
  permissions for a senatorial district. Any other missing role is created
  for the level of its areas. An officer role for a state, district, LGA or
  ward lets its holders see their area's incident counts, results and
  collation, start and join meetings, and use the forum. They cannot
  register, verify, appoint or change anything, and have no Situation Room.
  A role for polling units is an executive role like those of
  `import_executives`. Each new role reports to the coordinator of its level;
  administrators change them in Role Management.
- State, Deputy State, LGA and Ward Coordinators also receive, from the
  administrator, the power to appoint and manage the roles below them in
  their area: a state's or district's coordinators at LGAs, wards and units,
  an LGA's at wards and units, a ward's at its units. No system role carries
  that power (see docs/access-control.md, section 5).
- `place` is where the officer votes, as precisely as the list allows: a
  polling unit code (`21-03-03-018`), a ward code (`21-20-05`), an LGA code
  (`KE-ARGUNGU`), or the state's code (`KD`) when the list gives no place.
  It must be in the state. The member is registered there.
- `area` is what the role covers: a senatorial district (`SD/049/JG`), an
  LGA, a ward or a polling unit code, or empty for the whole state.
- `vin` is the voter's card VIN, stored on the membership when valid.
- Rows with the same phone number and the same name are one person with
  several roles: one member, one place, an appointment for each role. The
  same number under different names holds all those rows back.
- `hold` holds a row back with its reason, for example while the chapter
  confirms it. Rows are also held for an invalid, shared or registered phone
  number or VIN, an area outside the state, or a role that does not serve
  that level. As above: a dry run until `--commit`, rows named by line and
  S/N only, the phone as written is the first password, and the officers are
  recorded as verified on the chapter's word.

**People listed without a post.** `import_members` takes a CSV file with the
columns `sn`, `name`, `phone`, `place` and `hold`, and registers each person
as a member at their place, with no post:

```bash
uv run python manage.py import_members agents.csv --state JI \
    --by <NA access ID> --source "APC Jigawa State: Buji polling unit agents"
```

- First used for the Jigawa polling unit agent lists (18 October 2026). Their
  consent is recorded on the chapter's word, but their identity is not: an
  agent's accreditation, like any post, waits for the usual check.
- `place` works as for officers. The same person listed again at the same
  place is imported once; listed at two places, both rows are held. Rows are
  also held for a missing name, an invalid, shared or registered phone
  number, or a place outside the state. As above: a dry run until
  `--commit`, rows named by line and S/N only, and the phone as written is
  the first password.

**Signing in.** Members sign in at `/auth/login/` with their phone and a
6-digit PIN. Obvious PINs (`123456`, `111111` and so on) are refused. After
5 failed attempts (members and staff alike), the account locks for
15 minutes. Signed in, anyone changes their own PIN or password at
`/auth/password/`, which signs them out everywhere else.

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

## Security officers

The Situation Room sends incidents to security officers (decided on 17
October 2026).

- **Who they are:** members, signed in like everyone else, whose role holds
  `respondToSecurity`. A National Administrator creates such a role and
  appoints officers to it over their areas (a polling unit, ward, LGA or
  state). An administrator holds every power but is never offered as an
  officer.
- **Who sends:** one grant holding both `viewSituationRoom` and
  `assignIncident` over the incident's place: the Situation Room Director,
  the National Administrator, or a role given both. Coordinators who assign
  incidents in their area do not send to security, but those who see the
  incident follow what happens.
- **Nearest:** `nearest/` measures from the incident's GPS fix (else its
  polling unit, ward or LGA) to each officer: their live location while they
  share it for a project, if a real fix arrived in the last
  `LIVE_STALE_MINUTES`, else the area of their role (a polling unit's own
  location, else the middle of the located units in the area; a unit or ward
  with none borrows its LGA's). Officers whose distance is unknown come last.
  An officer busy with another incident is marked, and one who declined this
  incident is not offered again.
- **Sending:** one officer at a time. Sending escalates the incident and
  tells the officer by app notification and SMS (channels the server can
  carry now; see **Bulk messages**). The notice names the incident, its
  category, severity and place; the reporter's number stays in the app.
  Notices are in the officer's inbox, not among the sender's messages.
- **The officer:** accepts or declines (with a reason), then says they are on
  the way, have arrived and have resolved it (with what was done), which
  resolves the incident. They see the place, a point to navigate to and the
  reporter's name and phone. An officer who loses the role stops answering.
- **Standing down:** the Situation Room cancels with a reason, and resolving
  or closing the incident there stands the officer down too; either way the
  officer is told. Every step is on the incident's trail (`/events/`, action
  `security`).

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

## Live incident video

An incident's reporter can broadcast live video of it from the phone
(`live/start/`), and whoever can see the incident watches it
(`live/watch/`), unseen and without broadcasting. The video runs on
LiveKit (set up for meetings, docs/meetings.md); the server only signs
passes. Each incident shows its latest stream under `live`.

- **Recording:** with a bucket set, LiveKit records the stream to it as an
  MP4, and `manage.py collect_recordings` copies the finished recording
  onto the incident as video evidence, then deletes it from the bucket.
  Without a bucket the video is watched live but not kept (`recording`
  is `off`). Settings: `LIVE_RECORDING_S3_ENDPOINT`,
  `LIVE_RECORDING_S3_BUCKET`, `LIVE_RECORDING_S3_ACCESS_KEY`,
  `LIVE_RECORDING_S3_SECRET`, optionally `LIVE_RECORDING_S3_REGION`
  (`us-east-1`) and `LIVE_RECORDING_S3_FORCE_PATH_STYLE` (true, for MinIO).
  Any S3-compatible store works; LiveKit must be able to reach it.
- **Run `uv run python manage.py collect_recordings` every minute** (cron).
  It also ends streams still open after `LIVE_STREAM_MAX_MINUTES` (120).
- **Webhook:** set `https://<api host>/api/v1/livekit/webhook/` as the
  LiveKit project's webhook URL. A closed room then ends its stream at
  once, and a failed recording says so; unsigned events are refused.
- Recording costs LiveKit egress minutes, and each recording is stored
  with the incident's other evidence.

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
  manual entry cannot carry GPS and are not flagged for it.
- **Incidents from a polling unit require GPS** (since 15 October 2026): when
  every area where someone reports incidents is a polling unit (agents and
  polling unit executives), a report without a fix is refused. Each report
  is judged against its unit: `location.status` and `distance_to_pu_m`.
  Coordinators may still report without GPS, for example from a computer;
  theirs show `noGps`.
- **Learning polling unit locations.** INEC publishes no coordinates. When a
  ward, LGA, state or zonal coordinator confirms a check-in
  (`confirmAgentLocation`), a unit with no location takes that check-in's
  position. The fix must not be suspect and must be within 150 m accuracy.
  Later check-ins and results are measured against it. You cannot confirm
  your own check-in.
- **Results:** an app result that is `away` or `suspect` goes to
  `underReview`, with a note giving the distance.
- **Field duty:** checking in and sharing location need `fieldDuty`, which
  agent and observer accounts hold, and roles such as the polling unit
  executives'. A person checks in at the one polling unit where they hold
  it: an agent account's own unit, or their appointment's. Agents on the
  Ground counts them all, each at their unit.
- **Live location** is shared only during a project the person takes part
  in (see **Projects**): their app starts sharing by itself while it runs,
  and sharing ends with the project. Readings outside a duty session are
  dropped. Pings are deleted after `LOCATION_PING_RETENTION_DAYS` (30). Run
  `uv run python manage.py purge_location_pings` daily.

## Projects

A project is a scheduled period of field work, such as an election or
another assignment (decided on 17 October 2026). It is how TGCG watches the
work while it happens.

- **Scheduling:** only a National Administrator (`manageProjects`, protected)
  creates one, from the app's settings. It has a name, a brief, a start and an
  end (at most 31 days), the areas it covers, and optionally an election.
  Before it starts everything may change; once it runs, only the name, the
  brief, the end and the place check, and it can be cancelled with a reason.
- **Who sees it:** everyone with a role in its areas, while it is scheduled,
  on, or finished within a week. Ordinary members do not.
- **Taking part:** coordinators holding `assignProjectParticipants` (state,
  LGA and ward coordinators) assign people with a role to a place inside both
  the project's areas and their own, by default the person's own polling
  unit. The person accepts in their app, which is their agreement to sharing
  their location while the project runs, or declines; they may withdraw
  later, and a coordinator may take them out with a reason. Nobody is in two
  projects at the same time.
- **While it runs:** an accepted person's app shares their location without
  a switch, and asks them to turn GPS on. Outside a project nobody shares
  location: sharing switched on before projects existed ends when migration
  `tracking.0005` runs. With `check_place`, every reading is judged against
  their polling unit, so `monitor/` shows who is at their place, away, or not
  sharing.
- **The work counts for it:** incidents, field reports and results recorded
  by someone taking part carry the project. An election that a project names
  takes results only while one of its projects runs (from phones that were
  offline, captured then and sent within a day); other elections still go by
  their status. `elections/?accepting=true` lists those open now.
- Every change and every answer is audited.

## Polling unit mapping

INEC publishes no polling-unit coordinates, so coordinators (`confirmAgentLocation`)
pin each unit on site before election day:

- **Pinning requires all of these:**
  - standing at the entrance (`at_entrance`);
  - a signboard that matches the unit (`name_matches`);
  - GPS accuracy of `PIN_MAX_ACCURACY_M` (30 m) or better;
  - no mock-location app;
  - a photo of the unit.
- **If the signboard doesn't match,** or the unit can't be found or has
  moved, report a problem instead of pinning.
- **The first pin sets the location** (`pinned`). A pin by a different
  coordinator within `PIN_CONFIRM_RADIUS_M` (50 m) confirms it (`confirmed`).
- **A pin further away is a conflict.** The location does not change until a
  coordinator who did not make the pin accepts one.
- **Agent check-ins:** a confirmed check-in also counts as a first pin.
- **Progress:** `/mapping/progress/` reports what share of each area is
  mapped.

## Forum

The discussion forum is for everyone with a role (`viewDiscussionRoom`),
never ordinary members. Published posts are seen across Nigeria.

| Method | Path | Notes |
|---|---|---|
| GET | `/api/v1/forum/posts/` | The feed, newest first. `?show=mine` (own posts, any status), `?show=review` (posts waiting for you), `?assignment=` or `?assignments=true`, scope codes |
| POST | `/api/v1/forum/posts/` | Multipart: `body`, `assignment` (a tag), `files` (repeat for each picture or video), optional `level`/`code`, `client_id` |
| DELETE | `/api/v1/forum/posts/{id}/` | Its author deletes it: words and files go |
| POST | `/api/v1/forum/posts/{id}/approve/`, `reject/`, `hide/` | Moderators (`moderateDiscussion`) of the post's area; `reject` and `hide` need a `reason` |
| GET, POST | `/api/v1/forum/posts/{id}/replies/` | Replies, oldest first; a new one appears at once |
| POST, DELETE | `/api/v1/forum/replies/{id}/hide/`, `/api/v1/forum/replies/{id}/` | A moderator hides a reply (with a reason); its author deletes it |
| GET | `/api/v1/forum/attachments/{id}/` | A picture or video, for whoever sees its post |
| GET, PATCH | `/api/v1/forum/settings/` | `approval_required`; only a National Administrator changes it |
| GET | `/api/v1/forum/assignments/` | Assignment tags on published posts, most recent first |

- **Approval:** while `approval_required` is on (the default), a post made at
  a polling unit waits for the coordinator of that ward. Ward coordinators
  hold `moderateDiscussion` for their ward, and the National Administrator
  holds every permission. Posts made at a ward or above, and every reply,
  appear at once. Waiting posts stay waiting when approval is switched off.
- **Assignments:** a post may name the job it shows ("Market rally"); posts
  with the same tag, in any case, are listed together.
- **Files:** pictures (`.jpg`, `.jpeg`, `.png`, `.webp`, `.heic`) up to
  `FORUM_MAX_PHOTO_MB` (15) and videos (`.mp4`, `.mov`, `.3gp`, `.webm`) up to
  `FORUM_MAX_VIDEO_MB` (50), at most `FORUM_MAX_ATTACHMENTS` (10) and
  `FORUM_MAX_POST_MB` (100) together per post. Their content must match
  their type. They are stored under `forum/` in private storage and served
  only through the attachment endpoint. Nginx's `client_max_body_size` must
  allow a whole post (for example `110m` at the defaults).
- **Record:** approvals, refusals, hidden posts and replies, deleted posts
  and settings changes are in the audit log.

## Bulk messages

SMS, WhatsApp and app notifications to members (decided on 17 October 2026).

- **Who sends:** a National Administrator gives the power for each channel
  through roles: `sendSms`, `sendWhatsApp`, `sendPushNotifications`. A
  sender reaches only active members in the areas where they hold that
  channel's power; an administrator reaches everyone. Sends go out at once,
  without approval, and each is audited (`message.sent`).
- **Who receives:** a message names groups, and reaches everyone in any of
  them once:
  - `filter`: members in some areas (all of Nigeria when none), narrowed to
    people with a role, without one, or holding chosen roles (`roles`, by
    role code), by `gender`, and to checked identities (`verified_only`);
  - `project`: those taking part in a project;
  - `list`: one of the sender's saved lists (membership numbers, or the people
    some groups reach when it is saved).
- **Where it goes:** SMS to the member's phone number; WhatsApp to their
  WhatsApp number when they gave one (`/me/member/`), else their phone
  number; app notifications to every phone signed in to their account. Every
  message also appears in the member's inbox (`/me/messages/`).
- **Sending:** each send is queued per person and channel, and
  `uv run python manage.py deliver_messages` hands the queue over in
  batches: SMS through Termii's bulk API (500 numbers a request), WhatsApp
  through Termii one by one, notifications through Firebase Cloud Messaging.
  Run it every minute (systemd timer or cron); each run works for up to 55
  seconds. What a service refuses is recorded per person (`delivered`).
- **Settings** (server environment, never in the code):
  - `SMS_BACKEND=termii`, `TERMII_API_KEY`, `TERMII_SENDER_ID` (an approved
    sender name), and `TERMII_BULK_CHANNEL` (`generic` by default; `dnd`
    reaches numbers on Do Not Disturb once Termii approves the sender);
  - `WHATSAPP_BACKEND=termii`, with a WhatsApp number connected to the
    sender in Termii (WhatsApp may require approved templates for messages
    that start a conversation);
  - `PUSH_BACKEND=fcm` and `FIREBASE_SERVICE_ACCOUNT_FILE`, the path to the
    Firebase project's service account key (JSON), readable only by the API.
  - `console` (the default outside production) records messages without
    sending them. Production refuses it: WhatsApp and app notifications
    default to `termii` and `fcm` there.
  - A channel whose service is not set up (no Termii key, no Firebase key
    file) is left out of `broadcasts/roles/` and refused when sending, so
    nothing is marked sent that was not.

## Wallets

A member's wallet records what TGCG owes them (decided on 16 October 2026).
TGCG credits it; members do not deposit, and the money stays in TGCG's bank.

- Only a verified member (by a coordinator or automatically) has a wallet.
- Someone holding `creditWallets` for the member's area requests a payment,
  and a different person (not the same account or phone number) holding
  `approveWalletCredits` there approves or rejects it. Nobody pays
  themselves. One payment is at most ₦1,000,000.
- An approved payment becomes a ledger entry that is never changed or
  removed. Amounts are kept in kobo and shown in naira.
- The member names their payout account: a bank and a 10-digit account
  number. The account name is always the member's name from their PVC; it
  is not stored and cannot be sent. One account belongs to one member.
- Withdrawals wait for a bank API, which will pay out and confirm that the
  bank's name for the account matches the member's.
- Every request, decision and account change is audited, with only the last
  four digits of the account number.

## Quality checks

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run python manage.py makemigrations --check --dry-run
```
