from src.core.exceptions import FieldError, NotFoundError, ValidationError


def test_app_error_uses_class_default_message_when_none_given() -> None:
    error = NotFoundError()
    assert error.message == "Resource not found"
    assert error.code == "NOT_FOUND"
    assert error.details == []


def test_app_error_uses_the_given_message_when_provided() -> None:
    error = NotFoundError("course 123 does not exist")
    assert error.message == "course 123 does not exist"


def test_app_error_carries_field_details() -> None:
    details = [FieldError(field="email", message="not a valid email")]
    error = ValidationError("bad input", details=details)
    assert error.details == details
