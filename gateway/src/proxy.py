"""Path routing to the owning service, and the request forwarding itself.

Clients see one API at /api/v1; which service answers is invisible to them. The route
table is the gateway's entire job, so it is the one central list in the platform.

Most routes are a whole-segment path prefix. A few resources are nested under another
service's prefix — enrollments live in learning but are addressed as
``/courses/{id}/enrollments`` — so those are matched first, by exact pattern.
"""

import re

import httpx
from fastapi import Request, Response

from platform_core.exceptions import UpstreamTimeoutError, UpstreamUnavailableError
from platform_core.logging import current_request_id
from platform_core.middleware import REQUEST_ID_HEADER
from src.config import UpstreamSettings

# Hop-by-hop headers (RFC 9110 §7.6.1) describe one connection and must not be
# forwarded. host and content-length are recomputed for the new request/response.
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)
# httpx decodes compressed bodies, so the upstream's content-encoding no longer
# describes what the gateway sends back.
_DROP_FROM_RESPONSE = _HOP_BY_HOP | {"content-encoding"}


Route = tuple[re.Pattern[str], str]

_SEGMENT = r"[^/]+"


def _prefix(prefix: str) -> re.Pattern[str]:
    """Whole-segment prefix match: /api/v1/courses never captures
    /api/v1/courses-archive."""
    return re.compile(rf"{re.escape(prefix)}(?:/.*)?")


def route_table(*, api_prefix: str, upstreams: UpstreamSettings) -> list[Route]:
    """(pattern, base URL) pairs, first match wins — nested exceptions before the
    prefix that would otherwise swallow them."""
    p = re.escape(api_prefix)
    return [
        # learning resources nested under course's and identity's prefixes
        (re.compile(rf"{p}/courses/{_SEGMENT}/enrollments(?:/.*)?"), upstreams.learning_url),
        (re.compile(rf"{p}/users/{_SEGMENT}/enrollment-history"), upstreams.learning_url),
        (_prefix(f"{api_prefix}/enrollments"), upstreams.learning_url),
        # identity
        (_prefix(f"{api_prefix}/auth"), upstreams.identity_url),
        (_prefix(f"{api_prefix}/users"), upstreams.identity_url),
        (_prefix("/.well-known/jwks.json"), upstreams.identity_url),
        # course
        (_prefix(f"{api_prefix}/courses"), upstreams.course_url),
        (_prefix(f"{api_prefix}/modules"), upstreams.course_url),
        (_prefix(f"{api_prefix}/lessons"), upstreams.course_url),
        # notification, analytics
        (_prefix(f"{api_prefix}/notifications"), upstreams.notification_url),
        (_prefix(f"{api_prefix}/analytics"), upstreams.analytics_url),
    ]


def resolve_upstream(path: str, routes: list[Route]) -> str | None:
    for pattern, base_url in routes:
        if pattern.fullmatch(path):
            return base_url
    return None


def _forwarded_headers(request: Request) -> list[tuple[str, str]]:
    headers = [
        (name, value)
        for name, value in request.headers.items()
        if name.lower() not in _HOP_BY_HOP
        and name.lower() not in {"x-forwarded-for", REQUEST_ID_HEADER.lower()}
    ]
    # Services trust X-Forwarded-For from the gateway (uvicorn --forwarded-allow-ips),
    # so per-IP throttles in identity see the real client, not the gateway.
    client_ip = request.client.host if request.client else "unknown"
    prior = request.headers.get("x-forwarded-for")
    headers.append(("x-forwarded-for", f"{prior}, {client_ip}" if prior else client_ip))
    headers.append(("x-forwarded-proto", request.url.scheme))
    headers.append(("x-forwarded-host", request.headers.get("host", "")))
    # One request id from the edge to the service, so both access-log lines correlate.
    headers.append((REQUEST_ID_HEADER, current_request_id() or ""))
    return headers


async def forward(request: Request, client: httpx.AsyncClient, base_url: str) -> Response:
    url = base_url + request.url.path
    if request.url.query:
        url = f"{url}?{request.url.query}"

    try:
        upstream = await client.request(
            request.method, url, headers=_forwarded_headers(request), content=await request.body()
        )
    except httpx.TimeoutException as exc:
        raise UpstreamTimeoutError() from exc
    except httpx.TransportError as exc:
        raise UpstreamUnavailableError() from exc

    response = Response(content=upstream.content, status_code=upstream.status_code)
    for name, value in upstream.headers.multi_items():
        if name.lower() not in _DROP_FROM_RESPONSE:
            response.headers.append(name, value)
    return response
