"""Coarse token check at the edge: reject a bearer token whose signature doesn't verify
against identity's published keys, before it costs a service anything.

This is deliberately coarse. It checks the signature only — not expiry, roles or
revocation — because every service verifies again independently (CLAUDE.md §9) and a
client routinely sends an expired access token alongside POST /auth/refresh; rejecting
that here would break the refresh flow. Requests without a token pass through: each
service decides which of its routes are public.

Fetching identity's JWKS is the one synchronous cross-service call the architecture
allows. It is cached, and if identity is unreachable the gateway fails open — the
services still verify every token themselves.
"""

import time

import httpx
import jwt
import structlog

from platform_core.exceptions import UnauthorizedError

logger = structlog.get_logger(__name__)

# Upper bound on how often an unknown kid can force a JWKS re-fetch, so a stream of
# garbage tokens can't turn the gateway into a load generator against identity.
_MIN_REFETCH_INTERVAL_SECONDS = 30.0


class JWKSVerifier:
    def __init__(self, client: httpx.AsyncClient, *, jwks_url: str, ttl_seconds: float) -> None:
        self._client = client
        self._jwks_url = jwks_url
        self._ttl_seconds = ttl_seconds
        self._keys: jwt.PyJWKSet | None = None
        self._fetched_at = 0.0

    async def _fetch(self) -> None:
        self._fetched_at = time.monotonic()
        try:
            response = await self._client.get(self._jwks_url)
            response.raise_for_status()
            self._keys = jwt.PyJWKSet.from_dict(response.json())
        except (httpx.HTTPError, jwt.PyJWTError, ValueError) as exc:
            # Keep serving with the last known keys; a stale key set is still right
            # for every token signed before a rotation.
            await logger.awarning("jwks_fetch_failed", url=self._jwks_url, detail=str(exc))

    async def _key_for(self, kid: str | None) -> jwt.PyJWK | None:
        if time.monotonic() - self._fetched_at >= self._ttl_seconds:
            await self._fetch()
        key = self._find(kid)
        if key is None and time.monotonic() - self._fetched_at >= _MIN_REFETCH_INTERVAL_SECONDS:
            await self._fetch()
            key = self._find(kid)
        return key

    def _find(self, kid: str | None) -> jwt.PyJWK | None:
        if self._keys is None:
            return None
        return next((key for key in self._keys.keys if key.key_id == kid), None)

    async def reject_forged(self, authorization: str | None) -> None:
        if not authorization or not authorization.lower().startswith("bearer "):
            return
        token = authorization[len("bearer ") :].strip()

        try:
            kid = jwt.get_unverified_header(token).get("kid")
        except jwt.PyJWTError as exc:
            raise UnauthorizedError("Malformed bearer token") from exc

        key = await self._key_for(kid)
        if key is None:
            if self._keys is None:
                return  # identity never answered — fail open, services still verify
            raise UnauthorizedError("Token signed with an unknown key")

        try:
            # The algorithm comes from the published key, never from the token header.
            jwt.decode(
                token,
                key.key,
                algorithms=[key.algorithm_name],
                options={"verify_exp": False, "verify_aud": False},
            )
        except jwt.PyJWTError as exc:
            raise UnauthorizedError("Invalid token signature") from exc
