# 0009 — Courses module: basic CRUD slice, and ownership-check as a raw string

- Status: Accepted
- Date: 2026-09-22

## Scope

This is the week-1 deliverable ("User & Course management (basic CRUD)"), not the full
`docs/architecture/api-design.md` §5 course surface. Built: `POST /courses`,
`GET /courses` (catalogue browse with `?q=&level=&status=` and cursor pagination),
`GET /courses/{id}`, `PATCH /courses/{id}`, `DELETE /courses/{id}` (soft archive).
Not built: modules, lessons, prerequisites, the publishing workflow, enrollment — each
is its own row in the API design and has no caller yet, so building them now would be
exactly the "for later" scaffolding CLAUDE.md §13 rules out.

`Course.status` therefore only has `draft` / `published` / `archived` today — no
`publishing`/`failed` states, since nothing drives that state machine yet. `PATCH` never
touches `status`; only `DELETE` does, moving a course to `archived`. There is currently
no route that moves a course to `published` — the next slice to build.

## Ownership check compares against a raw string, not `users.models.UserRole`

CLAUDE.md §10 says role gating goes through `Depends(require_roles(...))`, never an
inline `if user.role == "admin"`. `PATCH`/`DELETE /courses/{id}` need "owning instructor
OR admin", which `require_roles` can't express — it can't see the resource, only the
token. That combined check has to live where CLAUDE.md §4 says ownership checks belong:
the owning module's service (`modules/courses/service.py::_can_modify`).

The check compares `role` against the plain string `"admin"`, not against an imported
`users.models.UserRole.ADMIN`. `CurrentUser.role` (`core/dependencies.py`) is already a
raw string decoded off the JWT `role` claim — courses has no other reason to import
anything from `users`, and doing so only for this one enum member would recreate the
same cross-module coupling ADR 0008 accepted for `auth`→`users`, without auth's
justification (a synchronous call that cannot go through an event). One literal string
standing in for a stable wire-format value (the JWT's `role` claim, not an internal
enum) was judged cheaper than the import.

**Consequence:** if `UserRole`'s values ever change, `modules/courses/service.py`'s
`_ADMIN_ROLE` constant needs updating by hand — it will not get a type error, only a
runtime one (an admin token failing an ownership check it should pass). Worth a search
for `_ADMIN_ROLE`-style literals across modules if that enum is ever touched.

## `Query(...)`/`require_roles(...)` and ruff's B008

`ruff`'s `flake8-bugbear` B008 rule flags any function call used as a parameter default,
including nested calls inside an already-whitelisted `Depends(...)`. Two fixes, both
applied: `fastapi.Query` was added to `extend-immutable-calls` in `pyproject.toml`
alongside the existing `fastapi.Depends` entry (same justification: FastAPI's own
documented pattern, not a bug), and `Depends(require_roles("instructor"))` — a
project-local call ruff has no way to know is safe — became a module-level singleton
(`_require_instructor = require_roles("instructor")`) referenced from `Depends(...)`
instead of called inline.

## `src/modules/courses/__init__.py` was missing on the first pass

`core/router.py` auto-discovery walks `pkgutil.iter_modules(modules_package.__path__)`,
which only surfaces `src/modules/*` entries that are actual Python packages — a
directory without `__init__.py` doesn't count, even though `routes.py` inside it imports
and runs fine standalone. The new module silently didn't appear in `/openapi.json` (no
error, no log line) until `__init__.py` was added. Caught by inspecting
`create_app().openapi()`'s path list directly rather than assuming the module was wired
up because the files existed and imported cleanly — worth checking for every future
module, since the failure mode is silence, not an exception.

## Consequences

- Any new module needs `__init__.py` from the start, or auto-discovery skips it with no
  error.
- Any future FastAPI `Depends(some_local_function(...))` pattern needs the same
  module-level-singleton treatment `_require_instructor` uses, not
  `extend-immutable-calls` — that list is for third-party calls ruff can't inspect, not
  a general B008 bypass.
- The next courses slice (publish workflow, modules/lessons) is where `Course.status`
  gains `publishing`/`failed` and a `POST /courses/{id}/publish` route — see API design
  §6 for the shape that's expected to slot in without changing today's response
  contract.
