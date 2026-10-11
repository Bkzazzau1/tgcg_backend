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
§7. Ordinary membership suspension cannot target a National Administrator
(Phase 4).

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

Checks recorded per member: PVC captured, PVC validated against an
authoritative source, duplicates, SMS phone verification, live selfie,
liveness, face comparison. Each check is passed, failed, pending or
unsupported. Automatic verification only when every required check has
passed. Today there is no authoritative PVC source, no liveness detection
and no trusted reference photo, so those checks are recorded as
**unsupported** and members go to coordinator review. Nothing is reported
as passed unless it was actually performed.

## 9. Sensitive data

Coordinators see members in their areas (name, status, roles, area,
verification status). Phone numbers, full VIN, PVC image, selfie and
verification evidence need `viewMemberSensitiveData` for that member's area,
enforced in the serializers.

## 10. Transition

A data migration creates the system role definitions from the former fixed
roles. Existing accounts keep their legacy access; nothing is deleted or
widened. An administrator links a staff account to a verified member, which
converts its legacy role into a normal assignment. Unknown roles map to no
access.

## 11. API

| Endpoint | Use |
|---|---|
| `GET /access/permissions/` | The permission registry (module, action, protected, sensitive). |
| `/access/roles/` | List, search and filter roles (with active appointment counts); `POST` to create; `PATCH /<code>/`; `POST /<code>/archive/` and `/restore/`. |
| `/access/assignments/` | Appointments in the caller's areas and those they supervise; `POST` to appoint (several areas, optional restriction and dates); `POST /<id>/suspend/`, `/reactivate/` (managers and supervisors), `/revoke/`, `/restrict/`; `GET /<id>/history/`. |
| `/access/delegations/` | Grants the caller made or received, or inside their delegating reach; `POST` to delegate; `POST /<id>/revoke/` (never cascades); `GET /<id>/history/`. |
| `/access/admin-actions/` | National Administrators only: `POST` to request suspending or removing an administrator (`target` access ID, `kind`, `reason`); `POST /<id>/approve/`, `/cancel/`. |
| `/access/administrators/` | National Administrators only: every administrator account with its state (active, suspended, inactive), legacy role, appointments and pending request; `POST /<access_id>/reinstate/`. |
| `GET /me/access/` | Where the signed-in user's access comes from (also in `/auth/me/` as `access`). |
| `/me/assignments/` | The member's own appointments; `POST /<id>/accept/`, `/decline/`, `/resign/`. |
| `GET /access/audit/` | The audit log (viewAudit held nationwide); `GET /access/audit/verify/` checks the hash chain (National Administrators). |

Every existing endpoint now checks the permission it uses against the
caller's grants and their areas (`within_scope_q`, `scope_contains` and
`default_to_actor_scope` refuse a user without a permission).

## 12. Status

| Phase | State |
|---|---|
| 1 Audit and design | Done (this document) |
| 2 Django foundation | Done: registry, role definitions, appointments with several areas, engine, all existing checks moved onto it, lifecycle, audit log, API, tests |
| 3 Delegation and protection | Done (backend): delegation grants with depth, term and chain rules, non-cascading revocation, self-elevation guards, supervisors, two-administrator approval, reinstatement, server recovery command, audit, API, tests |
| 4 Membership integration | Not started: verification checks and review, membership suspension/deactivation endpoints, polling-unit changes, sensitive fields |
| 5 Flutter integration | Not started |
| 6 Verification and cleanup | Not started |

Known limitations so far:

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
