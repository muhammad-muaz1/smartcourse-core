# CLAUDE.md — identity service

Root `CLAUDE.md` governs architecture, data rules, async tooling, auth, response contracts,
observability and coding standards. This file covers only what is specific to this service.

## What this service owns

User accounts, roles, credentials and sessions. It is the platform's only token issuer:
it holds the JWT private key, issues and rotates token pairs, and publishes the public keys
at `/.well-known/jwks.json` so every other service verifies tokens offline.

**Database:** `identity_db` (PostgreSQL). No other service reads it.
**Task queue:** none yet.
**Redis DB index:** `0` — login lockouts, register throttles, password-reset tokens,
access-token denylist.
**Port (local):** `8001`

## Modules

| Module | Responsibility |
| --- | --- |
| `auth` | register, login, refresh rotation with reuse detection, logout, `/auth/me`, password reset |
| `users` | the `User` model and lookups; admin user management endpoints land here |

## Events produced

None yet. `user.registered.v1`, `user.role_changed.v1` and `instructor.approved.v1` arrive
with the outbox (API design §12) — each written in the same transaction as the change.

## Events consumed

None.

## Service-specific rules

- The private key never leaves this service. Other services get the public key only.
- `auth` may import `users`' model and service directly (docs/adr/0008) — one aggregate,
  one lifecycle. No other cross-module import is allowed.
- Refresh-token reuse detection commits the family revocation *before* raising, against
  the usual "services don't commit" rule — otherwise the error response rolls it back
  (docs/adr/0008).
- The access-token denylist is checked only here. Other services honour a logout when
  the access token expires (≤15 minutes) — docs/adr/0010.
- Admin accounts come only from `make create-admin`; registration can never grant `admin`.
