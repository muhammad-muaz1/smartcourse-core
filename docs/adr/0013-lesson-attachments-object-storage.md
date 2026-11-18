# 0013 — Lesson attachments on S3-compatible object storage (MinIO)

- Status: Accepted
- Date: 2026-11-18
- Builds on: ADR 0011/0012 (course service, "content service publish steps — not done")

## Context

The instructor UI has always staged lesson attachments (PDF/DOC/PPT/XLS/TXT/MD/CSV/ZIP)
but had nowhere to send them: it showed "sent once the content service's asset upload
(`POST /assets`) is live." No endpoint, no storage, no table existed — uploads silently
went nowhere. This ADR adds the backend and wires the frontend.

## Decisions

**Assets live in the course service, as a new `assets` module — not a new service.** The
course service already owns lessons and their ownership/editability rules; an attachment
is course content. A separate service would duplicate that auth and add a database,
worker, compose entry and gateway upstream for one table. `assets` is its own module
(not folded into `courses`) because binary storage is a distinct concern; it references
courses and lessons only by id, through `courses`' service helpers.

**Bytes in object storage, metadata in Postgres.** The `assets` row carries
`storage_key`, size, sha256, content type and uploader; the bytes live under the key in
the storage backend. The row is the access-control anchor — a download is authorised
against the asset's course (published → anyone; otherwise owner/admin only), the same
visibility rule as `get_lesson`.

**MinIO, via an S3 client, behind a `Storage` protocol.** `modules/assets/storage.py`
defines `Storage` (`put`/`get`/`delete`) with two backends: `MinioStorage` (aioboto3) and
`LocalFilesystemStorage`. MinIO is self-hosted and S3-compatible, so the *same*
`MinioStorage` runs unchanged against AWS S3 or any S3 endpoint — only endpoint and
credentials differ. aioboto3 (async) keeps downloads streaming without blocking the event
loop. The local-filesystem backend is the default so tests and a bare checkout need
nothing running; local dev and compose set `BACKEND=minio`. MinIO runs as a downloaded
binary (`make minio`, API :9000, console :9001), matching how Kafka/Jaeger/Prometheus run
without Docker; the bucket is created by the app on first upload.

**Upload is a raw-body stream, not multipart.** `POST /assets?course_id=&filename=&lesson_id=`
sends the file as the request body; the browser sets `Content-Type` from the blob. This
avoids a `python-multipart` dependency and lets the body stream straight to storage under
a 50 MB cap (enforced mid-stream). One file per request; the client loops.

**Lifecycle is crash-safe in the safe direction.** A failed or over-size upload deletes
its partial bytes inline. A delete removes the row and schedules the byte-delete as a
response background task — after the request's transaction commits — so a rollback can
never leave a row pointing at missing bytes. The opposite (orphaned bytes) is the
tolerated failure and is only ever logged.

## Not done

- **Large video upload.** Videos (up to 2 GB in the UI) stay URL-only: they exceed the
  50 MB cap and the gateway buffers whole bodies. The fix is a **presigned PUT** straight
  from the browser to the bucket (bytes skip the gateway) — `MinioStorage` has a one-method
  seam for it (`presigned_put_url`).
- **Virus scanning / content inspection**, thumbnails, and per-asset download counts.
- **Lifecycle/orphan sweep** for bytes left by the tolerated failure above.
- **Serving to enrolled students only.** Download visibility is published-or-owner today,
  the same gap as lesson access (ADR 0011): course has no enrollment data yet.

## Consequences

- The course service depends on `aioboto3` and, when `BACKEND=minio`, on a running MinIO
  (`make minio`). `make install` pulls the client; `make env` appends the `STORAGE__*`
  keys. Local assets (`BACKEND=local`) write under `services/course/var/` (gitignored).
- New endpoints (course service, via the gateway's `/assets` route): `POST /assets`,
  `GET /assets/{id}`, `GET /assets/{id}/content`, `DELETE /assets/{id}`,
  `GET /lessons/{id}/assets`.
- The frontend uploads staged attachments after a lesson saves, lists existing ones, and
  downloads them with the bearer token (object URL), since a plain link can't be authed.
