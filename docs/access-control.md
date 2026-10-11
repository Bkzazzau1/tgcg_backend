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
verified and active) + the legacy role, if any. Each grant keeps its own
permissions and areas; they are never pooled, so a publish right in Kaduna
and a view right in Kano never become a publish right in Kano. List queries
use the same rule: `scope_q(user, P)` = OR of the areas of the grants that
include `P`. Default is deny; missing data denies.

Expiry and scheduling are evaluated at request time, so an expired
assignment stops working without any background job. Everything is read from
the database on each request, so suspensions, revocations, archiving and
membership suspension apply immediately, including to existing tokens.

## 4. Schema (new `access` app)

- `RoleDefinition`: code, name (unique, case-insensitive; "National
  Administrator" reserved), description, category, permitted levels,
  permissions (subset of the registry), supervising role (no cycles),
  `allows_assignment`, `allows_role_creation`, system/protected flags,
  status (active/archived) with who/when, creator, timestamps.
- `RoleAssignment`: member, role, restricted permissions (null = all of the
  role's; otherwise a subset — can only reduce), starts/ends, status
  (active, suspended, declined, resigned, revoked; scheduled/expired derived
  from dates), who granted it and under which authority (provenance), and
  who/when/why for each status change.
- `RoleAssignmentArea`: the areas of an assignment (`ScopedModel`).
- `DelegationGrant` + `DelegationGrantArea`: grantor, recipient, permission
  set, role categories, levels, areas, may re-delegate (with remaining
  depth), validity dates, status, provenance (the grantor's own source).
- `AdminActionRequest`: two-person approval for suspending or removing a
  National Administrator.
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
2. **An assignment whose role allows assignment / role creation**: within
   that assignment's areas and its effective permissions.
3. **A delegation grant**: within the grant's permissions, categories,
   levels and areas.

To appoint member M to role R over areas A, one source must cover all of:
R's (restricted) permissions, R's category, every area in A and its level.
The appointee must be a verified, active member and cannot be the actor. A
new role's permissions must fit inside the creator's source. A new grant
must fit inside the grantor's source at creation time and records it as its
provenance. Revoking a grant does not revoke grants made from it; those stay
valid until revoked or expired, because each was validated when created.

Protected rules: only a National Administrator can appoint another; this
cannot be delegated; no custom role can carry the protected permissions or
the reserved name; suspending or removing a National Administrator needs two
distinct administrators (neither the target), never removes the last one,
and runs in one transaction. Ordinary membership suspension cannot target a
National Administrator.

## 6. Supervision

A role may name a supervising role (no cycles). Holders of the supervising
role may view, suspend and reactivate assignments of the supervised role
where their areas overlap. Supervision grants nothing else. Resignations are
recorded in the audit log and shown to supervisors.

## 7. Identity verification

Checks recorded per member: PVC captured, PVC validated against an
authoritative source, duplicates, SMS phone verification, live selfie,
liveness, face comparison. Each check is passed, failed, pending or
unsupported. Automatic verification only when every required check has
passed. Today there is no authoritative PVC source, no liveness detection
and no trusted reference photo, so those checks are recorded as
**unsupported** and members go to coordinator review. Nothing is reported
as passed unless it was actually performed.

## 8. Sensitive data

Coordinators see members in their areas (name, status, roles, area,
verification status). Phone numbers, full VIN, PVC image, selfie and
verification evidence need `viewMemberSensitiveData` for that member's area,
enforced in the serializers.

## 9. Transition

A data migration creates the system role definitions from the former fixed
roles. Existing accounts keep their legacy access; nothing is deleted or
widened. An administrator links a staff account to a verified member, which
converts its legacy role into a normal assignment. Unknown roles map to no
access.

## 10. API (Phase 2)

| Endpoint | Use |
|---|---|
| `GET /access/permissions/` | The permission registry (module, action, protected, sensitive). |
| `/access/roles/` | List, search and filter roles (with active appointment counts); `POST` to create; `PATCH /<code>/`; `POST /<code>/archive/` and `/restore/`. |
| `/access/assignments/` | Appointments in the caller's areas; `POST` to appoint (several areas, optional restriction and dates); `POST /<id>/suspend/`, `/reactivate/`, `/revoke/`, `/restrict/`; `GET /<id>/history/`. |
| `GET /me/access/` | Where the signed-in user's access comes from (also in `/auth/me/` as `access`). |
| `/me/assignments/` | The member's own appointments; `POST /<id>/accept/`, `/decline/`, `/resign/`. |
| `GET /access/audit/` | The audit log (viewAudit held nationwide); `GET /access/audit/verify/` checks the hash chain (National Administrators). |

Every existing endpoint now checks the permission it uses against the
caller's grants and their areas (`within_scope_q`, `scope_contains` and
`default_to_actor_scope` refuse a user without a permission).

## 11. Status

| Phase | State |
|---|---|
| 1 Audit and design | Done (this document) |
| 2 Django foundation | Done: registry, role definitions, appointments with several areas, engine, all existing checks moved onto it, lifecycle, audit log, API, tests |
| 3 Delegation and protection | Not started: delegation grants, supervisors acting on supervised appointments, two-administrator approval |
| 4 Membership integration | Not started: verification checks and review, membership suspension/deactivation endpoints, polling-unit changes, sensitive fields |
| 5 Flutter integration | Not started |
| 6 Verification and cleanup | Not started |

Known limitations so far: agent check-ins and live positions are located by
the agent account's registered area; the audit log is readable only with a
nationwide grant; until Phase 3, authority to appoint comes only from a
National Administrator or an appointment holding `assignRoles`.
