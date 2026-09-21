from src.core.logging import bind_request_id
from src.core.response import CursorPage, decode_cursor, encode_cursor, success


def test_success_wraps_data_and_carries_request_id() -> None:
    bind_request_id("req-envelope")
    result = success({"foo": "bar"})
    assert result.data == {"foo": "bar"}
    assert result.meta.request_id == "req-envelope"
    assert result.meta.timestamp


def test_cursor_round_trips_through_encode_and_decode() -> None:
    payload = {"id": "abc-123", "sort_value": 42}
    cursor = encode_cursor(payload)
    assert decode_cursor(cursor) == payload


def test_cursor_is_opaque_base64_not_raw_json() -> None:
    cursor = encode_cursor({"id": "abc"})
    assert "{" not in cursor


def test_cursor_page_defaults_to_no_more_pages() -> None:
    page = CursorPage[str](items=["a", "b"])
    assert page.next_cursor is None
    assert page.has_more is False
