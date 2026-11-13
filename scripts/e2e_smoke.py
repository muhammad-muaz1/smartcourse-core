"""End-to-end smoke test of the event-driven flows, through the gateway, against a running
stack (`make dev` + Kafka + Temporal). Run via `make smoke`.

Creates uniquely named throwaway users and courses (…@example.com), then checks that
each flow crosses every service it should:

  register ──▶ user.registered ──▶ learning (roster name), notification (welcome), analytics
  publish  ──▶ Temporal saga ──▶ course.published ──▶ learning course_cache, analytics
  enrol    ──▶ enrollment.created ──▶ notification (course welcome, via Celery), analytics
  complete ──▶ enrollment.completed ──▶ notification, analytics (completion, time-to-complete)
  bad publish ──▶ saga compensates ──▶ workflow.failed ──▶ analytics failures

Needs an admin for the platform-wide metrics: it creates one with identity's
create_admin script. Exits non-zero on the first failed check.
"""

import asyncio
import os
import secrets
import subprocess
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

BASE = os.environ.get("SMARTCOURSE_GATEWAY", "http://localhost:8000") + "/api/v1"
ROOT = Path(__file__).resolve().parent.parent
RUN = secrets.token_hex(3)
PASSWORD = "correcthorse-" + secrets.token_hex(4)


def ok(message: str) -> None:
    print(f"  ok  {message}")


def fail(message: str) -> None:
    print(f"FAIL  {message}")
    raise SystemExit(1)


async def eventually[T](
    what: str, probe: Callable[[], Awaitable[T | None]], *, within: float = 30.0
) -> T:
    """Event-driven state arrives in seconds, not instantly: poll until it does."""
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        result = await probe()
        if result:
            return result
        await asyncio.sleep(0.5)
    fail(f"timed out after {within:.0f}s waiting for: {what}")
    raise AssertionError  # unreachable


class Api:
    def __init__(self, client: httpx.AsyncClient, token: str | None = None) -> None:
        self.client = client
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}

    async def call(
        self, method: str, path: str, *, json: Any = None, idempotent: bool = False, **params: Any
    ) -> httpx.Response:
        headers = dict(self.headers)
        if idempotent:
            headers["Idempotency-Key"] = str(uuid.uuid4())
        return await self.client.request(
            method, BASE + path, json=json, headers=headers, params=params or None
        )

    async def data(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self.call(method, path, **kwargs)
        if response.status_code >= 400:
            fail(f"{method} {path} → {response.status_code} {response.text[:300]}")
        return response.json()["data"] if response.content else None


async def register_and_login(client: httpx.AsyncClient, role: str) -> tuple[Api, str]:
    email = f"smoke-{RUN}-{role}@example.com"
    anon = Api(client)
    user = await anon.data(
        "POST",
        "/auth/register",
        json={
            "email": email,
            "password": PASSWORD,
            "full_name": f"Smoke {role.title()}",
            "role": role,
        },
    )
    tokens = await anon.data("POST", "/auth/login", json={"email": email, "password": PASSWORD})
    return Api(client, tokens["access_token"]), user["id"]


async def admin(client: httpx.AsyncClient) -> Api:
    email = f"smoke-{RUN}-admin@example.com"
    # create_admin is a CLI; run it off the event loop.
    await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable,
            "-m",
            "scripts.create_admin",
            "--email",
            email,
            "--full-name",
            "Smoke Admin",
            "--password",
            PASSWORD,
        ],
        cwd=ROOT / "services/identity",
        check=True,
        capture_output=True,
    )
    tokens = await Api(client).data(
        "POST", "/auth/login", json={"email": email, "password": PASSWORD}
    )
    return Api(client, tokens["access_token"])


async def build_course(
    instructor: Api, *, lessons: int, seats: int | None = 5
) -> tuple[str, list[str]]:
    course = await instructor.data(
        "POST",
        "/courses",
        json={
            "title": f"Smoke {RUN} {lessons}L",
            "description": "Week-3 smoke",
            "max_enrollments": seats,
        },
    )
    lesson_ids: list[str] = []
    if lessons:
        module = await instructor.data(
            "POST", f"/courses/{course['id']}/modules", json={"title": "M1"}
        )
        for i in range(lessons):
            lesson = await instructor.data(
                "POST",
                f"/modules/{module['id']}/lessons",
                json={
                    "title": f"L{i}",
                    "lesson_type": "text",
                    "body": {"content": f"# Lesson {i}"},
                },
            )
            lesson_ids.append(lesson["id"])
    return course["id"], lesson_ids


async def publish(instructor: Api, course_id: str, *, expect: str) -> dict[str, Any]:
    run = await instructor.data("POST", f"/courses/{course_id}/publish", idempotent=True)

    async def finished() -> dict[str, Any] | None:
        current = await instructor.data("GET", f"/courses/{course_id}/publish-runs/{run['id']}")
        return current if current["status"] in ("succeeded", "failed") else None

    result = await eventually(f"publish run {run['id']}", finished)
    if result["status"] != expect:
        fail(f"publish run ended {result['status']}, expected {expect}: {result.get('error')}")
    return result


async def main() -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        print(f"smoke run {RUN} against {BASE}")

        # --- identity → everyone ---------------------------------------------------------
        instructor, instructor_id = await register_and_login(client, "instructor")
        student, student_id = await register_and_login(client, "student")
        boss = await admin(client)
        ok("registered instructor + student, created admin")

        # --- publishing saga → course.published → learning ----------------------------------
        course_id, lesson_ids = await build_course(instructor, lessons=2)
        await publish(instructor, course_id, expect="succeeded")
        ok("publish saga succeeded (Temporal)")

        async def enrol() -> dict[str, Any] | None:
            response = await student.call(
                "POST", f"/courses/{course_id}/enrollments", idempotent=True
            )
            # course.published.v1 not consumed yet: unknown course, or still "publishing".
            if response.status_code == 404 or "publishing" in response.text:
                return None
            if response.status_code != 201:
                fail(f"enrol → {response.status_code} {response.text[:300]}")
            return dict(response.json()["data"])

        enrollment = await eventually("course_cache to learn about the course (Kafka)", enrol)
        total = enrollment["lessons_total"]
        ok(f"enrolled once learning consumed course.published.v1 (lessons_total={total})")
        if enrollment["lessons_total"] != 2 or enrollment["lessons_completed"] != 0:
            fail(f"progress not initialised from the published lesson list: {enrollment}")
        ok("progress initialised in the enrollment transaction (0/2)")

        duplicate = await student.call("POST", f"/courses/{course_id}/enrollments", idempotent=True)
        if duplicate.status_code != 409 or duplicate.json()["code"] != "ALREADY_ENROLLED":
            fail(f"duplicate enrollment → {duplicate.status_code} {duplicate.text[:200]}")
        ok("duplicate enrollment rejected by the unique index (409 ALREADY_ENROLLED)")

        # --- roster name snapshot from user.registered --------------------------------------
        async def named() -> bool:
            roster = await instructor.data("GET", f"/courses/{course_id}/enrollments")
            return any(item["student_name"] == "Smoke Student" for item in roster["items"])

        await eventually("roster to show the student's name (user.registered.v1 → learning)", named)
        ok("roster shows the student's name without calling identity")

        # --- progress → completion ------------------------------------------------------------
        for lesson_id in lesson_ids:
            progress = await student.data(
                "POST",
                f"/enrollments/{enrollment['id']}/lessons/{lesson_id}/complete",
                idempotent=True,
            )
        if progress["status"] != "completed" or progress["progress_percent"] != 100.0:
            fail(f"last lesson didn't complete the enrollment: {progress}")
        ok("completing the last lesson completed the enrollment (enrollment.completed.v1)")

        # --- notification via Kafka → Celery ---------------------------------------------------
        async def notified() -> list[str] | None:
            inbox = await student.data("GET", "/notifications/me")
            sent = sorted(n["kind"] for n in inbox["items"] if n["status"] == "sent")
            return sent if len(sent) >= 3 else None

        kinds = await eventually("3 notifications delivered by Celery", notified)
        if kinds != ["account_welcome", "course_completed", "course_welcome"]:
            fail(f"unexpected notifications: {kinds}")
        ok(f"notifications delivered by Celery: {', '.join(kinds)}")

        # --- failed saga → workflow.failed → analytics ----------------------------------------
        empty_id, _ = await build_course(instructor, lessons=0)
        failed_run = await publish(instructor, empty_id, expect="failed")
        ok(f"empty course: saga compensated at '{failed_run['failed_step']}'")

        # --- analytics projections ---------------------------------------------------------------
        async def counted() -> dict[str, Any] | None:
            dashboard = await instructor.data("GET", f"/analytics/courses/{course_id}")
            return dashboard if dashboard["completed"] == 1 else None

        dashboard = await eventually("analytics to project the completion", counted, within=60)
        ok(
            f"course dashboard: {dashboard['enrollments']} enrolled, completion rate "
            f"{dashboard['completion_rate']}, avg {dashboard['average_hours_to_complete']}h"
        )

        response = await boss.call("GET", "/analytics/overview")
        body = response.json()
        if not body["meta"].get("as_of"):
            fail("projection read without meta.as_of")
        overview = body["data"]
        if overview["total_students"] < 1 or overview["total_instructors"] < 1:
            fail(f"overview missing users: {overview}")
        ok(f"overview (as_of {body['meta']['as_of']}): {overview}")

        async def failure_seen() -> bool:
            summary = await boss.data("GET", "/analytics/failures", recent=50)
            return any(item["reference_id"] == failed_run["id"] for item in summary["recent"])

        # The failures endpoint caches for 60s; a fresh failure may take a cache cycle.
        await eventually("analytics to record the failed publish", failure_seen, within=90)
        ok("failed publish visible in /analytics/failures (workflow.failed.v1)")

        for path, params in (
            ("/analytics/enrollments", {"granularity": "day"}),
            ("/analytics/completion", {}),
            ("/analytics/time-to-complete", {}),
            ("/analytics/courses/popular", {"period": "7d"}),
            ("/analytics/students/course-load", {}),
        ):
            await boss.data("GET", path, **params)
        ok("every brief metric endpoint answers")

        forbidden = await student.call("GET", "/analytics/overview")
        if forbidden.status_code != 403:
            fail(f"student read platform overview → {forbidden.status_code}")
        ok("students can't read platform-wide metrics (403)")
        print("all checks passed")


if __name__ == "__main__":
    asyncio.run(main())
