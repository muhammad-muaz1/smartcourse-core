# CLAUDE.md — notification service

Root `CLAUDE.md` governs architecture, data rules, async tooling, auth, response contracts,
observability and coding standards. This file covers only what is specific to this service.

## What this service owns

Notification records, the recipient directory they are delivered to, and delivery
itself. It is authoritative for what a user has been told and whether it reached them.
It is the only service that keeps a user's email address — it is the one that sends.

**Database:** `notification_db` (PostgreSQL). No other service reads it.
**Redis DB index:** `4` — health only today.
**Celery broker:** RabbitMQ (compose); Redis DB `6` for local dev without RabbitMQ.
**Port (local):** `8004`
**Processes:** the API (`python -m src.main`), the event worker (`python -m src.worker`:
Kafka consumer + outbox relay), and the Celery worker with beat (`make celery`).

## Modules

| Module | Responsibility |
| --- | --- |
| `notifications` | recipients, records, delivery tasks, the sweep, the inbox API |

## Events produced

| Event | Emitted when | Notes |
| --- | --- | --- |
| `workflow.failed.v1` | a delivery exhausts its retries | outbox row in the transaction that marks it failed |

## Events consumed

| Event | From | What this service does |
| --- | --- | --- |
| `user.registered.v1` | identity | upsert recipient; account welcome |
| `enrollment.created.v1` | learning | course welcome |
| `enrollment.completed.v1` | learning | completion congratulations |

## Service-specific rules

- No send endpoint, ever. Records are written only by consumers (API design §9).
- A consumer enqueues Celery work only through `after_commit` — never inside its
  transaction. A lost enqueue is recovered by the beat sweep, not by redelivery.
- Delivery is idempotent: anything not `pending` is already settled. A task may run twice.
- A fact older than `delivery.max_age_hours` is recorded `skipped`, never sent — replays
  and backfills must not re-greet users.
- Never log an address or a body. Log the notification id.
