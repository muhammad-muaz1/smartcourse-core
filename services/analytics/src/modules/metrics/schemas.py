"""Metric response models — one per brief metric (Requirement §5), every one read from a
projection and returned with ``meta.as_of``."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

Granularity = Literal["day", "week", "month"]
Period = Literal["7d", "30d", "90d", "all"]


class Overview(BaseModel):
    """Total Students · Total Instructors · Total Courses Published."""

    total_students: int
    total_instructors: int
    total_courses_published: int
    total_enrollments: int


class EnrollmentBucket(BaseModel):
    period_start: datetime
    enrollments: int


class EnrollmentSeries(BaseModel):
    """New Enrollments Over Time. Counts enrollments *started* in each bucket, whatever
    became of them later."""

    granularity: Granularity
    start: datetime
    end: datetime
    course_id: uuid.UUID | None
    buckets: list[EnrollmentBucket]


class CompletionStats(BaseModel):
    """Course Completion Rate = completed / (enrollments − withdrawn): a student who
    withdrew left the cohort; one still active hasn't finished *yet* and counts against
    the rate until they do."""

    course_id: uuid.UUID | None
    enrollments: int
    withdrawn: int
    completed: int
    completion_rate: float


class TimeToComplete(BaseModel):
    """Average Time to Complete a Course, over completed enrollments only."""

    course_id: uuid.UUID | None
    completed: int
    average_hours: float | None
    average_days: float | None


class PopularCourse(BaseModel):
    course_id: uuid.UUID
    title: str
    enrollments: int


class PopularCourses(BaseModel):
    """Most Popular Courses: by new enrollments in the period."""

    period: Period
    items: list[PopularCourse]


class CourseLoad(BaseModel):
    """Average Courses per Student: current (not withdrawn) enrollments per student who
    has at least one, and per registered student."""

    students_with_enrollments: int
    registered_students: int
    current_enrollments: int
    average_per_enrolled_student: float
    average_per_registered_student: float


class CourseDashboard(BaseModel):
    course_id: uuid.UUID
    title: str
    status: str
    enrollments: int
    active: int
    withdrawn: int
    completed: int
    completion_rate: float
    average_hours_to_complete: float | None


class FailureGroup(BaseModel):
    source: str
    service: str
    subject: str
    count: int
    last_seen: datetime


class FailureItem(BaseModel):
    id: uuid.UUID
    source: str
    service: str
    subject: str
    reference_id: str | None
    step: str | None
    error: str
    occurred_at: datetime


class FailureSummary(BaseModel):
    """Failed Events / Workflow Issues: dead letters and workflows that gave up."""

    total: int
    groups: list[FailureGroup]
    recent: list[FailureItem]
