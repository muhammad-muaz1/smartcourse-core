from src.core.logging import bind_request_id, clear_request_context, current_request_id


def test_current_request_id_is_none_before_binding() -> None:
    assert current_request_id() is None


def test_bind_request_id_makes_it_readable() -> None:
    bind_request_id("req-1")
    assert current_request_id() == "req-1"


def test_clear_request_context_removes_it() -> None:
    bind_request_id("req-2")
    clear_request_context()
    assert current_request_id() is None
