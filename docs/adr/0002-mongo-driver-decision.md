# 0002 — Mongo driver: Beanie on native PyMongo async, not Motor

- Status: Accepted
- Date: 2026-09-17

## Context

`claude/CLAUDE.md` §4 flags a specific risk before any Mongo code is written: Motor has
historically been Beanie's driver, and Motor itself was on a deprecation path in favour
of PyMongo's native async client. The instruction was explicit — verify Beanie's
current driver before writing Mongo code, and record the finding here; if Beanie still
depends on Motor, use a thin typed repository over `pymongo.AsyncMongoClient` instead
of Beanie.

## Evidence gathered 2026-09-17

- Motor's own deprecation timeline: full support ends 2026-05-14 (one year after
  PyMongo Async's production release); critical bug fixes continue only until
  2027-05-14. MongoDB's official guidance is to migrate off Motor onto PyMongo's async
  driver while it's still supported.
- Beanie's own release notes: Beanie 2.0 replaced Motor with PyMongo's async client
  (`AsyncMongoClient`) as its driver. `beanie.init_beanie()` now takes a PyMongo
  `AsyncMongoClient` instance, not a Motor client.
- Confirmed directly against the current release: `pip index` / PyPI metadata for
  `beanie==2.2.0` (latest at time of writing) lists its Mongo dependency as
  `pymongo!=4.15.0,<5.0.0,>=4.11.0` in `requires_dist`. **`motor` does not appear
  anywhere in Beanie 2.2.0's dependency graph, including optional extras.** `pymongo`
  itself is at `4.18.1`, which ships `pymongo.AsyncMongoClient`.

## Decision

Use Beanie `>=2.2,<3` directly as the ODM for MongoDB-backed data (content chunks,
publish-run records, notification payloads, analytics projections, event archive). Do
**not** fall back to a hand-rolled repository over `AsyncMongoClient`.

The fallback condition CLAUDE.md set out ("if it still depends on a deprecated driver,
use a thin typed repository instead") does not trigger: Beanie has already made the
migration MongoDB is recommending, so adopting Beanie *is* adopting the
native-async-client path, not an alternative to it.

`db/mongo/` still gets a thin repository layer per the CLAUDE.md §3 folder structure
(`client.py`, `document_base.py`, `repository.py`, deterministic index registration,
migration runner) — but that layer wraps Beanie `Document` classes for consistency with
the Postgres repository pattern and to keep Beanie-specific query syntax out of
`service.py`, not to replace Beanie's driver integration.

## Consequences

- `pymongo>=4.11,<5` is a transitive constraint via Beanie, not a separate pin choice;
  it's listed in the dependency table for visibility, and its version bound tracks
  whatever Beanie's own `requires_dist` allows at upgrade time.
- If Beanie's driver dependency changes again before Phase 2 lands, re-run this check
  before writing the Mongo client module — the finding here is dated and versioned,
  not a permanent fact about the library.
- No motor package appears anywhere in `pyproject.toml`.
