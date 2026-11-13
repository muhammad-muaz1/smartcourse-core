"""Topic naming — part of the contract, so producer and consumers never spell it twice.

``smartcourse.<domain>.<event>.v<major>``; the partition key is always the aggregate id,
so every event about one aggregate lands on one partition, in order. Only the owning
service produces to its domain's topics. Two topics are platform-wide rather than owned
by a domain: ``workflow.failed.v1`` (any service reports a failed background workflow)
and the dead-letter topic (any consumer parks a message it could not handle).
"""

PREFIX = "smartcourse"


def topic_for(event_type: str) -> str:
    """``course.published.v1`` → ``smartcourse.course.published.v1``."""
    return f"{PREFIX}.{event_type}"


DEAD_LETTER_TOPIC = f"{PREFIX}.deadletter.v1"
