"""Requires Docker (testcontainers spins up real Postgres and Redis) — nothing here is
mockable without losing the thing actually being tested (rotation, reuse detection,
lockout counters all live in the real stores).
"""

import uuid
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from src.config import SecuritySettings
from src.core.exceptions import ConflictError, RateLimitedError, UnauthorizedError
from src.core.security import (
    access_token_denylist_key,
    create_access_token,
    decode_access_token,
    verify_password,
)
from src.db import postgres
from src.db import redis as redis_db
from src.modules.auth import service
from src.modules.users import service as users_service
from src.modules.users.models import UserRole


class _CapturingSender:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    async def send(self, *, to: str, subject: str, body: str) -> None:
        self.calls.append({"to": to, "subject": subject, "body": body})


@pytest.fixture
def security_settings(tmp_path: Path) -> SecuritySettings:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_path = tmp_path / "private.pem"
    public_path = tmp_path / "public.pem"
    private_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    public_path.write_bytes(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return SecuritySettings(
        jwt_private_key_path=str(private_path),
        jwt_public_key_path=str(public_path),
        jwt_kid="test-kid",
        failed_login_max_attempts=3,
        failed_login_lockout_seconds=60,
        register_throttle_per_hour=3,
    )


async def _register(
    settings: SecuritySettings,
    *,
    email: str = "student@example.com",
    role: UserRole = UserRole.STUDENT,
) -> uuid.UUID:
    async for session in postgres.get_session():
        user = await service.register(
            session,
            redis_db.get_redis(),
            email=email,
            password="correcthorse123",
            full_name="Test User",
            role=role,
            client_ip="203.0.113.1",
            settings=settings,
            sender=_CapturingSender(),
        )
        return user.id
    raise AssertionError("get_session yielded nothing")


class TestRegister:
    async def test_creates_a_user_with_a_hashed_password(
        self, db_and_redis: None, security_settings: SecuritySettings
    ) -> None:
        user_id = await _register(security_settings)

        async for session in postgres.get_session():
            user = await users_service.get_by_id(session, user_id)
            assert user is not None
            assert user.hashed_password != "correcthorse123"
            assert verify_password(password="correcthorse123", password_hash=user.hashed_password)

    async def test_duplicate_email_is_a_conflict(
        self, db_and_redis: None, security_settings: SecuritySettings
    ) -> None:
        await _register(security_settings, email="dupe@example.com")

        with pytest.raises(ConflictError):
            await _register(security_settings, email="dupe@example.com")

    async def test_sends_a_welcome_notification(
        self, db_and_redis: None, security_settings: SecuritySettings
    ) -> None:
        sender = _CapturingSender()
        async for session in postgres.get_session():
            await service.register(
                session,
                redis_db.get_redis(),
                email="welcome@example.com",
                password="correcthorse123",
                full_name="Welcome Test",
                role=UserRole.STUDENT,
                client_ip="203.0.113.2",
                settings=security_settings,
                sender=sender,
            )

        assert len(sender.calls) == 1
        assert sender.calls[0]["to"] == "welcome@example.com"

    async def test_throttles_by_ip_after_configured_attempts(
        self, db_and_redis: None, security_settings: SecuritySettings
    ) -> None:
        # register_throttle_per_hour=3 on this fixture's settings.
        for i in range(3):
            async for session in postgres.get_session():
                await service.register(
                    session,
                    redis_db.get_redis(),
                    email=f"throttle{i}@example.com",
                    password="correcthorse123",
                    full_name="Throttle Test",
                    role=UserRole.STUDENT,
                    client_ip="203.0.113.9",
                    settings=security_settings,
                    sender=_CapturingSender(),
                )

        with pytest.raises(RateLimitedError):
            async for session in postgres.get_session():
                await service.register(
                    session,
                    redis_db.get_redis(),
                    email="throttle-overflow@example.com",
                    password="correcthorse123",
                    full_name="Throttle Test",
                    role=UserRole.STUDENT,
                    client_ip="203.0.113.9",
                    settings=security_settings,
                    sender=_CapturingSender(),
                )


class TestLogin:
    async def test_correct_credentials_return_a_token_pair(
        self, db_and_redis: None, security_settings: SecuritySettings
    ) -> None:
        await _register(security_settings, email="login@example.com")

        async for session in postgres.get_session():
            user, tokens = await service.login(
                session,
                redis_db.get_redis(),
                email="login@example.com",
                password="correcthorse123",
                settings=security_settings,
            )
            assert user.email == "login@example.com"
            assert tokens.access_token
            assert tokens.refresh_token
            assert tokens.expires_in == security_settings.access_token_expires_minutes * 60

    async def test_wrong_password_is_unauthorized(
        self, db_and_redis: None, security_settings: SecuritySettings
    ) -> None:
        await _register(security_settings, email="wrongpw@example.com")

        with pytest.raises(UnauthorizedError):
            async for session in postgres.get_session():
                await service.login(
                    session,
                    redis_db.get_redis(),
                    email="wrongpw@example.com",
                    password="not-the-password",
                    settings=security_settings,
                )

    async def test_unknown_email_is_unauthorized_not_not_found(
        self, db_and_redis: None, security_settings: SecuritySettings
    ) -> None:
        # Same error as a wrong password — must not reveal whether the account exists.
        with pytest.raises(UnauthorizedError):
            async for session in postgres.get_session():
                await service.login(
                    session,
                    redis_db.get_redis(),
                    email="nobody@example.com",
                    password="whatever123",
                    settings=security_settings,
                )

    async def test_locks_out_after_max_failed_attempts(
        self, db_and_redis: None, security_settings: SecuritySettings
    ) -> None:
        await _register(security_settings, email="lockout@example.com")

        for _ in range(security_settings.failed_login_max_attempts):
            with pytest.raises(UnauthorizedError):
                async for session in postgres.get_session():
                    await service.login(
                        session,
                        redis_db.get_redis(),
                        email="lockout@example.com",
                        password="wrong",
                        settings=security_settings,
                    )

        with pytest.raises(RateLimitedError):
            async for session in postgres.get_session():
                await service.login(
                    session,
                    redis_db.get_redis(),
                    email="lockout@example.com",
                    password="correcthorse123",  # even the right password is locked out now
                    settings=security_settings,
                )

    async def test_successful_login_clears_the_failure_counter(
        self, db_and_redis: None, security_settings: SecuritySettings
    ) -> None:
        await _register(security_settings, email="clears@example.com")

        async for session in postgres.get_session():
            with pytest.raises(UnauthorizedError):
                await service.login(
                    session,
                    redis_db.get_redis(),
                    email="clears@example.com",
                    password="wrong",
                    settings=security_settings,
                )

        async for session in postgres.get_session():
            # Must still succeed — one failure is well under the lockout threshold, and
            # a later successful login should reset the counter, not just stop counting.
            await service.login(
                session,
                redis_db.get_redis(),
                email="clears@example.com",
                password="correcthorse123",
                settings=security_settings,
            )

        failures = await redis_db.get_redis().get("auth:failed_login:clears@example.com")
        assert failures is None


class TestRefreshTokens:
    async def _login(
        self, security_settings: SecuritySettings, email: str
    ) -> tuple[uuid.UUID, str]:
        user_id = await _register(security_settings, email=email)
        async for session in postgres.get_session():
            _, tokens = await service.login(
                session,
                redis_db.get_redis(),
                email=email,
                password="correcthorse123",
                settings=security_settings,
            )
            return user_id, tokens.refresh_token
        raise AssertionError("get_session yielded nothing")

    async def test_rotates_the_refresh_token(
        self, db_and_redis: None, security_settings: SecuritySettings
    ) -> None:
        _, refresh_token = await self._login(security_settings, "rotate@example.com")

        async for session in postgres.get_session():
            _, tokens = await service.refresh_tokens(
                session, raw_refresh_token=refresh_token, settings=security_settings
            )
            assert tokens.refresh_token != refresh_token

    async def test_reusing_a_rotated_token_revokes_the_whole_family(
        self, db_and_redis: None, security_settings: SecuritySettings
    ) -> None:
        """Regression test: the reuse-detection branch used to revoke the family and
        then raise, but raising rolled the transaction back (get_session rolls back on
        exception) and silently undid the revocation — the rotated-in token kept
        working. Fixed by committing before raising; this test would have caught it.
        """
        _, token_a = await self._login(security_settings, "reuse@example.com")

        async for session in postgres.get_session():
            _, tokens = await service.refresh_tokens(
                session, raw_refresh_token=token_a, settings=security_settings
            )
            token_b = tokens.refresh_token

        with pytest.raises(UnauthorizedError):
            async for session in postgres.get_session():
                await service.refresh_tokens(
                    session, raw_refresh_token=token_a, settings=security_settings
                )

        # token_b was issued by the same, now-compromised family and must be dead too.
        with pytest.raises(UnauthorizedError):
            async for session in postgres.get_session():
                await service.refresh_tokens(
                    session, raw_refresh_token=token_b, settings=security_settings
                )

    async def test_unknown_token_is_unauthorized(
        self, db_and_redis: None, security_settings: SecuritySettings
    ) -> None:
        with pytest.raises(UnauthorizedError):
            async for session in postgres.get_session():
                await service.refresh_tokens(
                    session, raw_refresh_token="not-a-real-token", settings=security_settings
                )


class TestLogout:
    async def test_revokes_the_refresh_family_and_denylists_the_access_token(
        self, db_and_redis: None, security_settings: SecuritySettings
    ) -> None:
        user_id = await _register(security_settings, email="logout@example.com")
        async for session in postgres.get_session():
            _, tokens = await service.login(
                session,
                redis_db.get_redis(),
                email="logout@example.com",
                password="correcthorse123",
                settings=security_settings,
            )

        # A real access token, so jti/exp match what create_access_token actually
        # produces (logout only needs the claims, not a request-issued token).
        access_token = create_access_token(
            user_id=user_id, role="student", settings=security_settings
        )
        payload = decode_access_token(access_token, settings=security_settings)

        async for session in postgres.get_session():
            await service.logout(
                session,
                redis_db.get_redis(),
                raw_refresh_token=tokens.refresh_token,
                access_token_jti=payload["jti"],
                access_token_expires_at=payload["exp"],
            )

        assert await redis_db.get_redis().exists(access_token_denylist_key(payload["jti"]))

        with pytest.raises(UnauthorizedError):
            async for session in postgres.get_session():
                await service.refresh_tokens(
                    session, raw_refresh_token=tokens.refresh_token, settings=security_settings
                )


class TestPasswordReset:
    async def test_forgot_password_for_unknown_email_does_not_send_anything(
        self, db_and_redis: None, security_settings: SecuritySettings
    ) -> None:
        sender = _CapturingSender()
        async for session in postgres.get_session():
            await service.forgot_password(
                session,
                redis_db.get_redis(),
                email="nobody@example.com",
                settings=security_settings,
                sender=sender,
            )
        assert sender.calls == []

    async def test_reset_password_changes_the_password_and_is_single_use(
        self, db_and_redis: None, security_settings: SecuritySettings
    ) -> None:
        await _register(security_settings, email="reset@example.com")
        sender = _CapturingSender()

        async for session in postgres.get_session():
            await service.forgot_password(
                session,
                redis_db.get_redis(),
                email="reset@example.com",
                settings=security_settings,
                sender=sender,
            )

        raw_token = sender.calls[0]["body"].split("Reset token: ")[1]

        async for session in postgres.get_session():
            await service.reset_password(
                session, redis_db.get_redis(), token=raw_token, new_password="brandnewpassword"
            )

        async for session in postgres.get_session():
            user = await users_service.get_by_email(session, "reset@example.com")
            assert user is not None
            assert verify_password(password="brandnewpassword", password_hash=user.hashed_password)
            assert not verify_password(
                password="correcthorse123", password_hash=user.hashed_password
            )

        with pytest.raises(UnauthorizedError):
            async for session in postgres.get_session():
                await service.reset_password(
                    session, redis_db.get_redis(), token=raw_token, new_password="whatever"
                )

    async def test_reset_password_revokes_outstanding_refresh_tokens(
        self, db_and_redis: None, security_settings: SecuritySettings
    ) -> None:
        await _register(security_settings, email="revoke-on-reset@example.com")
        async for session in postgres.get_session():
            _, tokens = await service.login(
                session,
                redis_db.get_redis(),
                email="revoke-on-reset@example.com",
                password="correcthorse123",
                settings=security_settings,
            )

        sender = _CapturingSender()
        async for session in postgres.get_session():
            await service.forgot_password(
                session,
                redis_db.get_redis(),
                email="revoke-on-reset@example.com",
                settings=security_settings,
                sender=sender,
            )
        raw_token = sender.calls[0]["body"].split("Reset token: ")[1]

        async for session in postgres.get_session():
            await service.reset_password(
                session, redis_db.get_redis(), token=raw_token, new_password="brandnewpassword"
            )

        with pytest.raises(UnauthorizedError):
            async for session in postgres.get_session():
                await service.refresh_tokens(
                    session, raw_refresh_token=tokens.refresh_token, settings=security_settings
                )

    async def test_invalid_reset_token_is_unauthorized(
        self, db_and_redis: None, security_settings: SecuritySettings
    ) -> None:
        with pytest.raises(UnauthorizedError):
            async for session in postgres.get_session():
                await service.reset_password(
                    session, redis_db.get_redis(), token="not-a-real-token", new_password="whatever"
                )
