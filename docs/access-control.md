# Roles, permissions, membership and delegation

Design for dynamic roles in TGCG-EMCOP. The Django backend decides every
permission; the Flutter app only shows what the server allows.

## 1. Audit (2026-10-11)

| Area | Today | Gap |
|---|---|---|
| Accounts | `accounts.User`: one fixed `role` (enum of 13) and one scope. Sign-in by access ID or phone; JWT with refresh blacklist; lockout after failed logins. | One role and one area per account. No API to create or manage staff accounts (Django admin / `createsuperuser` only). |
| Permissions | `core.permissions`: `Capability` codes, fixed `ROLE_CAPABILITIES` per role, `requires()` checks "any of" at view level. | Static; derived from the role name. No per-assignment permissions, no restrictions. |
| Geography | `ScopedModel` stores a level plus the unit chain; `within_scope_q` / `scope_contains` compare against the actor's single scope (80 call sites in 13 files). | Cannot express several areas, or "this permission only in that area". |
| Members | `membership.Member`: PVC identity, phone (SMS-verified), selfie, PVC image, registered polling unit (scope), optional login account. Self and assisted registration. | `status` never reaches "verified": no identity-verification state, checks or review. No membership suspension/deactivation. No polling-unit change requests. Staff can see phone numbers without a separate permission. |
| Accreditation | `AccreditedAgent`: member + role (7 field roles) + one scope + lifecycle (pending/approved/suspended/revoked) + event history. | One active accreditation per member; fixed roles; does not grant any sign-in access. |
| Audit | Per-feature histories (accreditation events, result histories). `viewAudit` capability exists. | No central, tamper-evident audit log. |
| Flutter | `TgcgPermissionPolicy` mirrors the role matrix; the session already stores the server's capabilities; module visibility uses the static matrix. Role Assignment page edits local demo data only. | Navigation must follow server permissions; role management screens must use the server. |

What already works and is reused: membership registration (both paths,
deduplication by phone and VIN, SMS codes, selfie and PVC image), the
geography hierarchy and `ScopeRef`, JWT sessions and blacklist, the offline
sync outbox (field records only), accreditation for field roles.

## 2. Concepts

- **Member**: the organisational identity (`membership.Member`). Every
  operational role holder must be a member whose identity is verified and
  whose membership is active. A member signs in with their own account
  (`Member.user`). A National Administrator is also a member.
- **Role definition**: a named set of permission codes, the geographic levels
  it may be assigned at, a category and an optional supervising role. System
  roles mirror the 12 former fixed roles; National Administrator is
  protected.
- **Role assignment**: one member holding one role over one or more areas,
  with its own lifecycle, dates and optional restrictions. A member can hold
  many assignments, including the same role in several places.
- **Delegated authority**: a grant letting a recipient create roles and/or
  appoint members within a permission set, role categories, areas and levels.
- **Legacy access**: an existing account's fixed `User.role` keeps working
  during the transition, evaluated exactly as before (its own single scope),
  and is shown as legacy until the account is linked to a verified member.

## 3. Authorization rule

An action `P` on a resource in area `X` is allowed only when **one single**
grant allows both:

```
allowed(user, P, X) =
    user is active
    and membership is active (not suspended/deactivated)
    and ∃ grant g in active_grants(user):
            P ∈ g.permissions                 # role ∩ assignment restriction
            and ∃ area a ∈ g.areas: a contains X
```

`active_grants` = active, in-date assignments of active roles (member
verified and active) + active, in-date delegation grants (their
administrative powers and `viewRoles` only, in their own areas) + the legacy
role, if any. Each grant keeps its own permissions and areas; they are never
pooled, so a publish right in Kaduna and a view right in Kano never become a
publish right in Kano. List queries use the same rule: `scope_q(user, P)` =
OR of the areas of the grants that include `P`. Default is deny; missing data
denies.

Expiry and scheduling are evaluated at request time, so an expired
assignment stops working without any background job. Everything is read from
the database on each request, so suspensions, revocations, archiving and
membership suspension apply immediately, including to existing tokens.

## 4. Schema (new `access` app)

- `RoleDefinition`: code, name (unique, case-insensitive; "National
  Administrator" reserved), description, category, permitted levels,
  permissions (subset of the registry; holding `assignRoles` or
  `manageRoles` is what lets a role appoint or create roles), supervising
  role (no cycles), system/protected flags, status (active/archived) with
  who/when, creator, timestamps.
- `RoleAssignment`: member, role, restricted permissions (null = all of the
  role's; otherwise a subset — can only reduce), starts/ends, status
  (active, suspended, declined, resigned, revoked; scheduled/expired derived
  from dates), who granted it and under which authority (provenance: the
  appointing assignment or delegation grant), and who/when/why for each
  status change.
- `RoleAssignmentArea`: the areas of an assignment (`ScopedModel`).
- `DelegationGrant` + `DelegationGrantArea`: grantor, recipient (a member),
  permission set, role categories, levels, areas, may re-delegate (with
  remaining depth), validity dates, status with who/when/why revoked,
  provenance (the grantor's own source: administrator, assignment or grant).
- `AdminActionRequest` + `AdminActionApproval`: two-person approval for
  suspending or removing a National Administrator (one pending request per
  administrator, one approval per approver).
- `AuditEvent`: append-only, hash-chained log of security events (actor,
  target, action, scope, before/after without sensitive values, reason,
  provenance).
- On `Member`: `verification_status` (unverified, pending review, verified,
  rejected) with `VerificationCheck` records; `membership_state` (active,
  suspended, deactivated) with reason/who/when; `PollingUnitChangeRequest`.

## 5. Authority to create, assign and delegate

An actor's authority sources, each checked on its own:

1. **National Administrator** (protected system role, national area): any
   role, any area, except protected actions that need two administrators.
2. **An assignment holding the power** (`assignRoles`, `manageRoles`,
   `manageAssignments`, `delegateAuthority`): within that assignment's areas
   and up to its effective permissions; any role category or level.
3. **A delegation grant**: within the grant's permission ceiling, role
   categories, levels, areas and remaining depth. A grant gives its
   recipient the powers it names (and the right to see appointments in its
   areas), never the operational permissions in its ceiling.

One source must cover the whole of a request:

| Action | Needs | The source must cover |
|---|---|---|
| Create a role | `manageRoles` | the role's permissions, category and every permitted level |
| Edit, archive or restore a role | `manageRoles`, as its creator or in a supervising appointment (§6) | as above, plus the areas of every current holder whenever the change reaches them (new permissions, a new reporting line, archiving, restoring) |
| Appoint | `assignRoles` | the appointment's (restricted) permissions, the role's category, every area and its level |
| Suspend, reactivate, revoke | `manageAssignments` | what the appointment carries, its category, areas and levels (or the actor supervises it, §6, for suspend/reactivate) |
| Narrow a restriction | `manageAssignments` | everything the role could carry |
| Lift or widen a restriction | `manageAssignments` and `assignRoles` | as above, and what appointing it as it would become needs |
| Delegate | `delegateAuthority` | the grant's permissions, categories, levels, areas, and a depth below the source's |

Passing on an administrative power (in a role, an appointment or a grant)
is delegation: the source must itself hold `delegateAuthority`. Below a
National Administrator, further delegation passes on only through a grant,
where depth is counted, never by putting `delegateAuthority` into a role.
Whatever passes on administrative powers may not outlast its source: a grant,
an appointment carrying such powers, or a role change giving such powers to
current holders, must end no later than a source that ends. National
Administrator appointments are never restricted (every one is a full one).

Self-protection: nobody appoints, restricts or changes their own
appointment, delegates to themselves, or widens or restores a role they hold
(that would carry powers they hold in one area into the areas of the other
holding). "Themselves" includes another account with the same phone number.
Authority never acts on someone it came from: no appointment, grant,
management action or role widening can reach anyone upstream in its
source's chain of grantors and appointers. The appointee or recipient must be
a verified, active member. A restored role must be restored by whoever
archived it or by a National Administrator.

A new grant is validated against the grantor's authority when it is made
and records that source as its provenance. Revoking a grant does not revoke
grants or appointments made from it; those stay valid until revoked or
expired, because each was validated when created. A grant can be revoked by
a National Administrator or by anyone above it in its chain whose own
account and membership are in good standing (its grantor included, even
after the grantor's own authority has ended).

Protected rules: only a National Administrator can appoint another; this
cannot be delegated; no custom role can carry the protected permissions or
the reserved name; suspending or removing a National Administrator follows
§7. Membership suspension, deactivation and identity review refuse a
National Administrator (§8, §9).

## 6. Supervision

A role may name a supervising role (no cycles). The reporting line is
between roles, not places: a State Media Director in Kaduna can report to a
National Media Director. Holders of any role above another in its reporting
line may, within the areas of their own holding:

- see its appointments (any of the appointment's areas inside theirs);
- suspend an appointment (all of its areas inside one holding of theirs),
  and lift a suspension they made themselves; a suspension by a manager or
  administrator is lifted by a manager or administrator;
- edit the role's definition, if the supervising appointment itself holds
  `manageRoles` covering the role as it would become (and its holders, §5).

Supervision gives nothing else: no revoking, restricting or appointing, no
action on whoever appointed the supervisor, and no access to the supervised
role's operational records beyond what the supervisor's own role allows.
Changing a reporting line once a role has holders needs authority over all
of them. Suspensions by a supervisor are audited with the supervising role as
provenance. Resignations are recorded in the audit log and visible to
supervisors in the appointment list (state `resigned`).

## 7. National Administrators

**Two-administrator rule.** Suspending or removing a National Administrator
starts with a request by another administrator, with a reason. The request
counts as its requester's approval; it runs when a second, different
administrator approves. The administrator concerned can neither request nor
approve it, nobody approves twice, and there is at most one pending request
per administrator. Accounts sharing a phone number count as one person. At
execution the approvals are counted again: only approvers who are still
active administrators (and not the one concerned) count. A request is refused
unless at least two administrators other than the one concerned are active,
and execution refuses to leave no active administrator. Every request,
approval, cancellation, reinstatement and appointment of an administrator
first takes one lock (`national-administrators`) and only then checks that
the actor still is an administrator, so concurrent requests cannot together
remove everyone and a suspended administrator cannot finish one more action.
Each appointment changed this way gets its own audit event as well.

- Suspend: a legacy administrator account is deactivated; administrator
  appointments are suspended. Remove: the legacy role becomes `member`;
  administrator appointments are revoked. In both cases the account's
  refresh tokens are blacklisted, and its access stops at the next request.
- The requester can cancel a pending request (any administrator can once
  the requester is no longer one).
- Reinstating a suspended administrator, like appointing one, takes a single
  administrator other than the one concerned, and no pending request.

**Recovery procedure.** The two-administrator rule is never bypassed by the
API. When fewer than three administrators are active, two can no longer
approve action on a third; if none of those left can be trusted or reached
to appoint more, an operator with server access runs:

```
python manage.py recover_administrator --member TGCG-000123 \
    --reason "Why, and who in the party approved it" --confirm
```

Without `--confirm` it only checks. It appoints one verified, active member
with an account as National Administrator (authority `recovery`), only
while fewer than three are active, never suspends or removes anyone, and
writes `administrator.recovered` to the audit log with the operator's login
name. Run it only on a written decision of the party leadership, then use
the normal workflow.

## 8. Identity verification

**Who must be verified** (decided on 12 October 2026, to keep joining
simple for people who find forms hard):

- **Members: nobody.** Every member, verified or not, has the whole member
  app (card, polling unit and directions, news, volunteering, polling unit
  changes), and their membership card is valid.
- **Agents and observers: always.** A legacy agent or observer account acts
  only for a member whose identity is verified; an account that belongs to
  no member does nothing. Check-ins, duty and live location refuse an
  unverified agent, coordinators cannot confirm such an agent's check-in,
  and coverage counts only agents who can work. Approving an agent's
  accreditation needs a verified member too.
- **Roles: always.** Appointments and delegated authority need a verified
  member before they are made and at every request, so a member never sees
  or uses a role before verification, and loses it when verification is
  withdrawn.

`/auth/me/` reports `identity_verified`, so the app can tell a member listed
as an agent that the work starts once their identity is checked; until
then they get the member app.

Checks recorded per member (`VerificationCheck`, one current result per
check, each passed, failed, pending or unsupported):

| Check | How it is decided today |
|---|---|
| `pvcCaptured` | Passed when the VIN and polling unit code came with a photo of the card; pending when they were typed in without one, or with a VIN only; failed without a PVC, or when a USSD registration skipped the VIN. |
| `cardMatch` | The server reads the stored card photo itself (`pvc_reading`: Google Cloud Vision when configured, else Tesseract) and passes when the name given (every word, in any order, a letter or two misread allowed; titles such as Alhaji ignored) and the VIN are both on it. The VIN may differ by two characters, misread, missed or added, since two readers see it (the phone when the member joined, and this server); the name must be there as well, and another card's VIN differs in far more. When three to six characters differ, the detail says so: the VIN was probably misread when the member joined, and a reviewer compares it with the card. Pending without a card photo, when they are not found, or when the photo could not be read. After the comparison changes, `record_verification_checks --awaiting --recompare` reads again the cards that did not match and reports the outcomes as counts. The result keeps a keyed fingerprint of the name, VIN and photo it compared, never the details or the text read; once any of them changes it no longer counts, and the card is read again. This shows the details came from the card, not that the card is genuine. |
| `pvcValidated` | **Unsupported**: no authoritative INEC source is connected. Reading a PVC is not validating it. |
| `duplicates` | Phone and VIN are unique by design; pending when someone else has the same name and date of birth (a count only, as they may be outside the reviewer's area), or when there is no date of birth to compare. |
| `phoneVerified` | Passed when a code sent by SMS was entered, or when the mobile network supplied the number during USSD registration. |
| `selfieCaptured` | Passed when a selfie was captured, noted as not biometric verification; failed without one. |
| `liveness` | **Unsupported**: no liveness detection. |
| `faceMatch` | **Unsupported**: no trusted reference photo exists to compare with. |

Checks run at registration, when a member adds their gender, date of
birth, selfie or PVC scan, and on demand (`POST .../verification/check/`,
or `manage.py record_verification_checks`: `--awaiting` for every member
awaiting a decision, `--dry-run` to count first). For a member awaiting a
decision, the card photo is read only when no comparison covers their
name, VIN and photo as they are.

**Automatic verification** (TGCG's policy, decided on 16 October 2026): a
member awaiting a decision is verified without a person when `pvcCaptured`,
`cardMatch`, `selfieCaptured` and `duplicates` have all passed and a gender
and date of birth are given. The decision has no reviewer
(`verified_automatically` in `.../verification/`) and is audited as
`member.verifiedAutomatically`, naming the policy and every check. It
counts as any verification: agent work, roles and the wallet. It is not
proof of identity: `pvcValidated`, `liveness` and `faceMatch` stay
unsupported, never passed. Everyone else goes to **pendingReview**.

A coordinator holding `verifyMemberIdentity` for the
member's area reviews in person, records notes and approves (the checks are
run again first; refused while any has failed) or rejects. State, LGA and
ward coordinators hold `verifyMemberIdentity` for their own area (decided on
13 October 2026), so the agents there can be verified close to home; zonal
coordinators do not. Nobody reviews themselves, anyone their authority came
from (§5), or a National Administrator. Rejection, like any loss of
verification, stops every appointment at once, so the same authority must
also cover every area where the member holds a role (appointments not
ended, delegated authority, a staff account's area): a ward coordinator
cannot decide the identity of an LGA role holder who lives in the ward;
the LGA coordinator can. Changing a verified member's name or date of birth
sends them back to review, so it needs `verifyMemberIdentity` as well as
`manageMembership`, and a reason; a National Administrator's identity
details are never changed through the API. Every run and decision is in the
audit log with whoever caused it; check details never hold the VIN, phone
number or images. A member's history (`/members/<number>/history/`) is
for those holding `verifyMemberIdentity` or `suspendMembers` there.

## 9. Membership standing and registered polling unit

Holders of `suspendMembers` for a member's area can suspend (reason
required; all operational access stops at once, sessions are ended),
deactivate (reason required; the account can no longer sign in) and
restore a membership. Appointments stay on record and only those still
otherwise valid authorize anything after a restore. Nobody acts on their
own membership or on anyone their authority came from, and these endpoints
refuse National Administrators (§7). A suspended member shares no location,
cannot check in, start duty or volunteer, and is left out of live maps and
coverage. A deactivated member cannot set a PIN to sign in again, and their
membership card scans as not valid.

A member's registered polling unit comes from their PVC. After an INEC
transfer the member asks to move (`/me/polling-unit/changes/`), sending
the DELIM code and a photo of the updated card. A coordinator holding
`reviewPollingUnitChanges` for the requested unit checks the card and
approves or rejects (reason required). Approval moves the registered
polling unit and keeps the new card image; it never changes appointments.
Staff edits cannot move a PVC holder (only a National Administrator can). A
member can make at most five requests in 30 days, and withdrawals are
audited too.

## 10. Sensitive data

Coordinators see members in their areas: name, membership and
verification status, roles, area. Phone number, email, VIN (even masked),
date of birth, photo and disabilities come back as `null` (with
`sensitive_visible: false`) unless the caller holds `viewMemberSensitiveData` for that
member's area; the same applies to the member photo endpoint, the phone of
volunteers and the registered phone of agents. Finding a member by phone
(`?phone=`) only searches those areas. Legacy coordinator roles were given
this permission explicitly, because they already saw these details. The
updated-card photo of a polling unit change is shown to its reviewers, as
the evidence for that decision. A staff member scanning a membership card's
QR code still sees the holder's photo, to compare with the person in front
of them. Members always see their own details.

## 11. Transition

A data migration creates the system role definitions from the former fixed
roles. Existing accounts keep their legacy access; nothing is deleted or
widened, except that legacy coordinators now hold `viewMemberSensitiveData`
explicitly for what they already saw. Unknown roles map to no access.
Since 12 October 2026, agent and observer accounts act only when they
belong to a verified member (§8).
Planned, not built yet: an administrator links a staff account to a
verified member, converting its legacy role into a normal assignment.

## 12. API

| Endpoint | Use |
|---|---|
| `GET /access/permissions/` | The permission registry (module, action, protected, sensitive), readable by anyone signed in: it holds no personal data. |
| `/access/roles/` | List, search and filter roles (with active appointment counts); `POST` to create; `PATCH /<code>/`; `POST /<code>/archive/` and `/restore/`. |
| `/access/assignments/` | Appointments in the caller's areas and those they supervise; `POST` to appoint (several areas, optional restriction and dates); `POST /<id>/suspend/`, `/reactivate/` (managers and supervisors), `/revoke/`, `/restrict/`; `GET /<id>/history/`. |
| `/access/delegations/` | Grants the caller made or received, or inside their delegating reach; `POST` to delegate; `POST /<id>/revoke/` (never cascades); `GET /<id>/history/`. |
| `/access/admin-actions/` | National Administrators only: `POST` to request suspending or removing an administrator (`target` access ID, `kind`, `reason`); `POST /<id>/approve/`, `/cancel/`. |
| `/access/administrators/` | National Administrators only: every administrator account with its state (active, suspended, inactive), legacy role, appointments and pending request; `POST /<access_id>/reinstate/`. |
| `GET /me/access/` | Where the signed-in user's access comes from (also in `/auth/me/` as `access`, with `membership_number` when the account belongs to a member and `supervises` when it holds a role others report to). |
| `/me/assignments/` | The member's own appointments; `POST /<id>/accept/`, `/decline/`, `/resign/`. |
| `GET /access/audit/` | The audit log (viewAudit held nationwide); `GET /access/audit/verify/` checks the hash chain (National Administrators). |
| `/members/` | Members in the caller's areas, with `verification_status`, `membership_state`, `roles` and personal details only where allowed; filter by both states; `?phone=`. `POST /<number>/suspend/`, `/deactivate/`, `/restore/`; `GET /<number>/history/`. |
| `/members/<number>/verification/` | `GET` the decision and every check; `POST check/` to run them again, `approve/` (notes) or `reject/` (reason). |
| `/polling-unit-changes/` | Requests to move into the caller's areas; `POST /<id>/approve/`, `/reject/`; `GET /<id>/card/` (the updated card photo). |
| `/me/polling-unit/changes/` | The member's own requests; `POST` (multipart: `delim`, `pvc_image`, `reason`); `POST /<id>/withdraw/`. `/me/member/` now includes `verification_status` and `membership_state`. |
| `/projects/` | Scheduled field work (see the README's **Projects**): everyone with a role sees those overlapping their areas. Creating, changing and cancelling need `manageProjects` (protected: National Administrators only). `participants/` assigns people with a role, with `assignProjectParticipants` (state, LGA and ward coordinators) over the place; `monitor/` needs that or `viewLiveOperations`, and shows only the caller's areas. A person shares location only while a project they accepted runs. |
| `/broadcasts/` | Bulk SMS, WhatsApp and app notifications (see the README's **Bulk messages**). Each channel is its own power, `sendSms`, `sendWhatsApp` and `sendPushNotifications`, which no role holds until a National Administrator gives it; a sender reaches only active members in the areas where they hold the channel's power. `/message-lists/` are each sender's own. |
| `/incidents/<reference>/security/` | Security officers (see the README's **Security officers**): the Situation Room, one grant holding `viewSituationRoom` and `assignIncident` over the incident's place, picks an officer from `nearest/` and sends it; whoever sees incidents there follows each step. Officers hold `respondToSecurity`, which no role holds until a National Administrator gives it, and answer at `/me/security/`. |
| `/me/registered/` | Anyone signed in registers members with a name and a phone number (see the README's **Member registration**); no permission is needed and there is no limit. Each person sees and completes only the members they registered, until the identity is checked; every change is audited against them. |

Every existing endpoint now checks the permission it uses against the
caller's grants and their areas (`within_scope_q`, `scope_contains` and
`default_to_actor_scope` refuse a user without a permission).

## 13. Status

| Phase | State |
|---|---|
| 1 Audit and design | Done (this document) |
| 2 Django foundation | Done: registry, role definitions, appointments with several areas, engine, all existing checks moved onto it, lifecycle, audit log, API, tests |
| 3 Delegation and protection | Done (backend): delegation grants with depth, term and chain rules, non-cascading revocation, self-elevation guards, supervisors, two-administrator approval, reinstatement, server recovery command, audit, API, tests |
| 4 Membership integration | Done (backend): verification checks at registration and coordinator review, membership suspension, deactivation and restore, polling-unit change requests, personal-detail restrictions, verified identity required to approve agents, audit, API, tests |
| 5 Flutter integration | Done: navigation and every in-app check follow the server's permissions (`session.can`), members holding appointments get the operations app with a "Your access" summary, Roles & Access (roles, appointments, delegations, administrators, audit log), Member Review (identity review, polling unit changes, standing), My access (accept, decline, resign), and polling unit change requests in the member app |
| 6 Verification and cleanup | Done: see **Verification** below |

### Verification

- Each of the brief's required test cases has at least one automated test,
  including concurrency-sensitive rules (approvals are re-counted under the
  administrators' lock), session revocation (real JWTs stop working when a
  membership ends; refresh tokens are blacklisted) and privilege escalation
  (self-appointment by a second account, authority flowing back up its
  chain, widening a held role, lifting restrictions, delegation depth and
  term, supervisors undoing a superior).
- Independent adversarial reviews of Phases 3, 4 and 5; every confirmed
  finding was fixed, and all but two small interface ones (end dates that
  cover the whole chosen day, closing My access once the last appointment
  ends) have a regression test.
- Client contract tests replay the exact requests the Flutter client builds
  (`apps/access/tests/test_client_contract.py`).
- Flutter widget tests cover the new screens at desktop and phone width,
  server-driven permissions, and members with appointments signing in to
  the operations app; module visibility per legacy role is unchanged.
- Checks run before every commit: `ruff check`, `ruff format --check`,
  `manage.py check`, `makemigrations --check`, OpenAPI validation with no
  warnings, the full backend suite on a fresh database, `flutter analyze`
  and the full Flutter suite.
- True concurrency (two transactions at once) needs PostgreSQL; the tests run
  on SQLite, so the locking is verified by design and by re-checks under the
  lock, not by parallel transactions.

Known limitations:

- In offline presentation mode the app still uses its built-in role matrix
  and demo data; the role screens need a server connection, and no change
  to roles or access is ever queued offline.
- A member appointed to the Polling Unit Agent role uses the operations app.
  Checking in at a polling unit and sharing location still need an agent
  account (the field app and tracking serve the field roles' accounts).
- An operator with appointments in several areas works in the smallest area
  containing them all (Nigeria when they span zones); every request is still
  checked per appointment by the server.
- Agent check-ins and live positions are located by the agent account's
  registered area.
- The audit log is readable only with a nationwide grant.
- Legacy fixed roles give no authority to appoint or delegate (only a
  legacy National Administrator does).
- There is no per-user notification system, so supervisors learn of
  resignations from the appointment list and the audit log, not a message.
- One administrator can appoint another (as the brief requires), so the
  two-administrator rule stops a lone administrator acting directly, not one
  who first appoints an accomplice; the audit log records both steps. Two
  accounts of one person are recognised only when they share a phone number.
- Row locks (`select_for_update`) take effect on PostgreSQL; the SQLite
  database used in development and tests serialises writes instead.
- Legacy agent and staff accounts not linked to a member cannot be suspended
  as members; their account must be deactivated instead.
- An agent account must be the member's own account given the agent role,
  which today is done in the Django admin. Planned: approving an agent's
  accreditation gives the member's account the agent role.
- Existing before this work: asking for a registration code
  (`POST /register/code/`) answers differently for a phone number or VIN
  that is already registered, so it reveals whether someone is a member.

## 14. Role menus

The fixed roles follow the role grid agreed on 10 October 2026 (the
"Proposed" view of https://claude.ai/artifact/1gk5S1xZuSLgbmPDMQU5yp). The
server holds each role's permissions (`ROLE_CAPABILITIES`, copied by the
app's `permissions.dart` for presentation mode), and the app's menu
(`allowedModulesFor` in `session.dart`) follows the permissions, so a screen
a role does not see is also refused by the server. Since 13 October 2026:

- Four screen permissions: `viewLiveOperations` (Live Operations and Agents
  on the Ground; the live agents and coverage endpoints require it),
  `viewAlerts` (Alert Centre), `viewAnalytics` (AI Data Analytics) and
  `manageGeography` (Geographic Operations, the master data: National
  Administrator only). Custom roles show these screens only when they
  include the new permissions.
- Technical Support sees no election data: no situation room, incidents or
  media intelligence. It keeps communications, the forum, meetings, System
  Monitoring and Data & Governance.
- The Executive Viewer sees summaries: no evidence, forum or audit log.
- The National Collation Officer and Situation Room Director no longer read
  the audit log; the National Collation Officer and LGA coordinators lose
  Media Intelligence.
- Menu rules: Member Enrolment opens for everyone who registers members (all
  coordinators); Field Monitoring for those who report from the field; AI
  Verification for result verifiers and membership managers; System
  Monitoring for the administrator and Technical Support.
- State, LGA and ward coordinators verify members' identities in their area
  (`verifyMemberIdentity`, §8), so they see Member Review with its Identity
  review tab. A ward coordinator's area ends at the ward, and nobody decides
  the identity of someone whose role reaches beyond their area; zonal
  coordinators do not verify.

Since 15 October 2026 the Situation Room is national. The National
Administrator, Situation Room Director, National Collation Officer, Legal
Officer and Executive Viewer hold `viewSituationRoom`, and anyone else gets
it only from a role an administrator gives them. Zonal, state, LGA and ward
coordinators lost it; they keep their area's incidents in Field Monitoring,
and Agents on the Ground through `viewLiveOperations` (the agent check-in
list also accepts `confirmAgentLocation`, which they hold). The polling unit
executive roles imported that day lost it too (`remove_role_permission`).

The forum (15 October 2026) is for everyone with a role. Agents and
observers now start discussions too (`createDiscussionThread`), since
anyone with a role at a polling unit may post. A post made at a polling unit
waits for the ward coordinator, who holds the new `moderateDiscussion` for
their ward (approve, refuse, hide), while the forum's one approval switch is
on; only a National Administrator changes it.

Field duty (15 October 2026): checking in at a polling unit and sharing
location on duty now go by the `fieldDuty` permission instead of the agent
and observer account types. Those accounts hold it, and so do roles such as
the polling unit executives', who work as their unit's agents; a person
checks in at the one polling unit where they hold it.

Security officers (17 October 2026) are members whose role holds
`respondToSecurity`; their menu shows Security Response, the incidents the
Situation Room sends them. They sign in on the same page as everyone else.
The National Administrator holds the power like every other but is never
offered as an officer.

Registered members per area (`GET /members/counts/`) are for whoever may
find members (`MEMBER_READERS`: the administrator, coordinators, and those
who verify identities), and only for an area wholly within their reach, so a
count is never a partial one. The same rule covers members and exco per area
(`GET /members/analysis/` and `/members/exco/`, 17 October 2026), and the
printable list of an area's members and exco (`GET /members/list-pdf/`, 19 October
2026), whose every download is recorded in the audit log. The Members
& Exco screen (`membershipNetwork`) is the National Administrator's, through
`manageMembership`; the dashboard shows each account's own area when it may
see all of it.

Not done yet:

- Observers have their agreed screens, but in the command centre. Moving
  them to the phone field app waits until the field app works from server
  data instead of local agent profiles.
- Role Assignment for zonal and state coordinators (appointing within their
  area) needs cascading appointments, and Technical Support's support tools
  (unlocking accounts, resetting PINs) do not exist yet.
