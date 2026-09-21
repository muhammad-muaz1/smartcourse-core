# 0008 — Auth/users module boundary, and two SQLAlchemy/Postgres storage gotchas

- Status: Accepted
- Date: 2026-09-21

## Auth depends on users' model and service directly

CLAUDE.md §4 says modules never import another module's `service.py` or `models.py` —
cross-module work goes through events or a deliberately exposed function. `auth` and
`users` break this, on purpose: `modules/auth/service.py` imports
`modules/users/service.py` and calls `users_service.get_by_email` /
`users_service.get_by_id` / `users_service.create_user` directly.

Login cannot go through an event bus — it's a synchronous request that needs the
account row *now*, not eventually. This is the same coupling NestJS itself has
(`AuthModule` depends on `UsersModule`, not the other way around, and not through
events) for the same reason. The rule holds for peer business modules (courses
shouldn't reach into enrollments) where the coupling isn't forced by the nature of the
operation; auth/users is the one place it is.

`core/dependencies.py`'s `get_current_user`, by contrast, does **not** import
`modules/users` — it only decodes the JWT and checks a Redis denylist, returning a
`CurrentUser` built entirely from token claims (`id`, `role`, `jti`, `expires_at`). That
keeps the actual layering rule (`core/` never imports `modules/`) intact. The
consequence: a role change or deactivation takes effect on next login/refresh, not
mid-session — an accepted tradeoff of a 15-minute access token. `GET /auth/me` (which
does need the live row) fetches it itself, inside the auth module, exactly the way any
other auth→users call does.

## `sa.Enum(SomeEnum)` stores `.name`, not `.value`, by default

Verified empirically before applying the migration: `Enum(UserRole, name="user_role")`
with no further options creates a Postgres enum with labels `STUDENT`, `INSTRUCTOR`,
`ADMIN` — the Python enum members' *names*, uppercase. Every other place a role
appears (JWT `role` claim, JSON responses) uses `UserRole.STUDENT`'s *value*,
`"student"`, because `UserRole` is a `StrEnum` and `json.dumps` serializes it via its
string value. Left alone, the database would have stored uppercase role names while
every other representation of the same role was lowercase — a mismatch nothing would
have caught until something compared the two directly (e.g. a raw SQL query filtering
`role = 'student'`, which would silently match zero rows).

Fixed with `values_callable=lambda enum_cls: [m.value for m in enum_cls]` on the column
definition, confirmed against the compiled DDL before generating the migration
(`col_type.enums` came back `['student', 'instructor', 'admin']`). Caught before the
first migration was ever applied to a real database — the wrong version was generated,
inspected, downgraded, deleted, and regenerated rather than shipped and patched later
with a data migration.

## `Mapped[datetime]` defaults to a naive `TIMESTAMP` column, not `TIMESTAMPTZ`

Discovered by actually running `POST /auth/register` against the real database, not by
reading the code: it failed with `asyncpg.exceptions.DataError: can't subtract
offset-naive and offset-aware datetimes`. `TimestampMixin` used
`datetime.now(tz=UTC)` correctly (CLAUDE.md §8's rule), but `mapped_column()` with no
explicit type argument maps `Mapped[datetime]` to a plain `DateTime` column —
`TIMESTAMP WITHOUT TIME ZONE` on Postgres. asyncpg refuses to bind a timezone-aware
Python `datetime` against a naive column outright, so this wasn't a silent data
problem, it was a hard 500 on every insert.

Fixed with `mapped_column(DateTime(timezone=True), ...)` on every datetime column
(`TimestampMixin.created_at/updated_at`, and `RefreshToken.expires_at/revoked_at`,
which aren't covered by the mixin). Migration regenerated and reapplied against the
real database to confirm the fix — `sa.DateTime(timezone=True)` now appears in the
generated DDL for every timestamp column.

**A second gotcha found while re-generating that migration:** downgrading a migration
that creates a Postgres native enum type does not drop the type — only the table.
`alembic downgrade` followed by `alembic upgrade` failed with
`DuplicateObjectError: type "user_role" already exists` until the migration's
`downgrade()` was given an explicit `sa.Enum(name="user_role").drop(op.get_bind(),
checkfirst=True)` after `op.drop_table(...)`. Verified with an actual
downgrade→upgrade cycle after the fix, not just by reasoning about it.

## Consequences

- Any future `sa.Enum(SomePyEnum)` column in this codebase needs the same
  `values_callable`, or it silently reintroduces this mismatch. Worth a lint/review
  habit, not just this one call site.
- Any future `Mapped[datetime]` column outside `TimestampMixin` needs
  `mapped_column(DateTime(timezone=True))` explicitly — the mixin doesn't help a
  module-specific datetime field like `RefreshToken.expires_at`. Same review habit.
- Any future migration involving a Postgres native enum needs the same explicit
  `sa.Enum(name=...).drop(...)` in `downgrade()` — autogenerate never adds it.
- The auth→users coupling means `modules/users/` exists now with only `models.py` and
  `service.py` — no `routes.py` yet, since no user-facing users endpoints
  (list/detail/update/role-change from API_DESIGN.md §4) were in scope for this change.
  Auto-discovery only picks up modules that define a `routes.py`, so this is inert
  until that module's own endpoints are built, not a half-finished module.
