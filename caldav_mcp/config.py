"""Environment configuration, read once at startup.

Conventions, shared with the sibling carddav-mcp:

* A switch that turns a protection **on** (``CALDAV_MCP_READ_ONLY``) is read
  tolerantly (``1``/``true``/``yes``, any case, surrounding whitespace). A typo
  there must not silently leave the server unprotected.
* A switch that **lifts** a protection (``CALDAV_MCP_ALLOW_NO_AUTH``,
  ``CALDAV_ALLOW_PLAINTEXT``, ``CALDAV_MCP_CALDAV_VERIFY_SSL=false``) is read
  strictly: anything but the exact spelling leaves the protection in place, and
  an unrecognised value refuses to start rather than guess.
* A list variable that is set but empty refuses to start. Whoever wrote
  ``CALDAV_ALLOW_TOOLS=`` meant to restrict something, and opening everything
  is the one wrong answer.
* Secrets are removed from the environment once read, so child processes and
  ``/proc/<pid>/environ`` never see them.
"""

from __future__ import annotations

import ipaddress
from collections.abc import MutableMapping
from dataclasses import dataclass
from datetime import UTC, tzinfo
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from caldav_mcp.sanitize import quoted

DEFAULT_MAX_EVENTS = 50
MAX_MAX_EVENTS = 500
MIN_API_KEY_LENGTH = 16
DEFAULT_TIMEOUT_SECONDS = 20

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_TRUE = frozenset({"1", "true", "yes"})
_FALSE = frozenset({"0", "false", "no"})


class ConfigError(ValueError):
    """The environment is unusable. The message is safe to print."""


@dataclass(frozen=True)
class Config:
    """Validated, immutable server configuration."""

    url: str
    username: str
    password: str
    api_key: str
    allow_no_auth: bool
    port: int
    path: str
    tz: tzinfo
    tz_name: str
    week_start: int
    verify_ssl: bool
    ca_bundle: str
    tls_cert: str
    tls_key: str
    read_only: bool
    allow_tools: str | None
    deny_tools: str | None
    default_calendar: str
    max_events: int
    timeout: int
    trust_proxy: bool
    rate_limit_max_failures: int
    rate_limit_window_seconds: int

    def __repr__(self) -> str:
        """Never leak secrets through a stray log line."""
        return f"Config(url={self.url!r}, username={self.username!r}, ...)"


def _tolerant_on(raw: str | None) -> bool:
    return (raw or "").strip().lower() in _TRUE


def _strict_true(raw: str | None) -> bool:
    return (raw or "").strip() == "true"


def _int(env: MutableMapping[str, str], name: str, default: int, lo: int, hi: int) -> int:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        value = lo - 1
    if not lo <= value <= hi:
        raise ConfigError(f"{name} must be an integer between {lo} and {hi} - got {quoted(raw, 40)!r}.")
    return value


def _is_loopback(hostname: str) -> bool:
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _validate_url(url: str, *, allow_plaintext: bool) -> str:
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        parsed.port  # noqa: B018 - raises ValueError on an out-of-range port
    except ValueError:
        # Deliberately not echoed: an unparsable value may still carry a password.
        raise ConfigError("CALDAV_URL is not a valid URL.") from None
    if parsed.scheme not in ("http", "https") or not hostname:
        raise ConfigError("CALDAV_URL must be an http:// or https:// URL with a host.")
    if parsed.username or parsed.password:
        raise ConfigError("CALDAV_URL must not contain credentials - use CALDAV_USERNAME and CALDAV_PASSWORD.")
    if parsed.query or parsed.fragment:
        raise ConfigError("CALDAV_URL must not carry a query string or a fragment.")
    if parsed.scheme == "http" and not _is_loopback(hostname) and not allow_plaintext:
        raise ConfigError(
            "CALDAV_URL uses plain http to a non-local host, so the password and every event would be sent "
            "unencrypted. Use https://, or set CALDAV_ALLOW_PLAINTEXT=true if this network is trusted end to end."
        )
    return url


def _timezone(name: str) -> tuple[tzinfo, str]:
    name = name.strip()
    if not name or name.upper() == "UTC":
        return UTC, "UTC"
    try:
        return ZoneInfo(name), name
    except ZoneInfoNotFoundError, ValueError:
        # A wrong zone silently shifts every "today" and "tomorrow", so refuse.
        raise ConfigError(f"TZ {quoted(name, 40)!r} is not an IANA zone name such as 'Europe/London'.") from None


def _week_start(raw: str | None) -> int:
    value = (raw or "monday").strip().lower()
    if value not in _WEEKDAYS:
        raise ConfigError(f"CALDAV_WEEK_START must be a weekday name such as 'monday' - got {quoted(value, 20)!r}.")
    return _WEEKDAYS.index(value)


def _verify_ssl(raw: str | None) -> bool:
    value = (raw if raw is not None else "true").strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigError(f"CALDAV_MCP_CALDAV_VERIFY_SSL must be true or false - got {quoted(value, 20)!r}.")


def _optional_list(env: MutableMapping[str, str], name: str) -> str | None:
    raw = env.get(name)
    if raw is None:
        return None
    if raw.strip() == "":
        raise ConfigError(f"{name} is set but empty. Remove the variable, or name the tools.")
    return raw.strip()


def load_config(env: MutableMapping[str, str], *, scrub: bool = True) -> Config:
    """Validate *env* and build a :class:`Config`.

    With ``scrub`` (the default) the password, username and API key are removed
    from *env* before anything can fail, so a refusal to start leaves nothing
    behind either.
    """
    url = env.get("CALDAV_URL", "").strip()
    username = env.get("CALDAV_USERNAME", "")
    password = env.get("CALDAV_PASSWORD", "")
    api_key = env.get("CALDAV_MCP_API_KEY", "").strip()
    if scrub:
        for name in ("CALDAV_PASSWORD", "CALDAV_USERNAME", "CALDAV_MCP_API_KEY"):
            env.pop(name, None)

    missing = [
        n for n, v in (("CALDAV_URL", url), ("CALDAV_USERNAME", username), ("CALDAV_PASSWORD", password)) if not v
    ]
    if missing:
        raise ConfigError(f"missing required environment variable(s): {', '.join(missing)}")
    url = _validate_url(url, allow_plaintext=_strict_true(env.get("CALDAV_ALLOW_PLAINTEXT")))

    allow_no_auth = _strict_true(env.get("CALDAV_MCP_ALLOW_NO_AUTH"))
    if not api_key and not allow_no_auth:
        raise ConfigError(
            "CALDAV_MCP_API_KEY is not set, so the endpoint would accept anyone. Set a key of at least "
            f"{MIN_API_KEY_LENGTH} characters, or set CALDAV_MCP_ALLOW_NO_AUTH=true to run without one."
        )
    if api_key and len(api_key) < MIN_API_KEY_LENGTH:
        raise ConfigError(f"CALDAV_MCP_API_KEY must be at least {MIN_API_KEY_LENGTH} characters.")

    path = env.get("CALDAV_MCP_PATH", "/mcp").strip() or "/mcp"
    if not path.startswith("/"):
        raise ConfigError(f"CALDAV_MCP_PATH must start with '/' - got {quoted(path, 40)!r}.")
    if path.rstrip("/") == "/health":
        raise ConfigError("CALDAV_MCP_PATH must not be /health, which is the unauthenticated probe route.")

    tls_cert = env.get("CALDAV_MCP_TLS_CERT", "").strip()
    tls_key = env.get("CALDAV_MCP_TLS_KEY", "").strip()
    if bool(tls_cert) != bool(tls_key):
        raise ConfigError("CALDAV_MCP_TLS_CERT and CALDAV_MCP_TLS_KEY must be set together.")

    tz, tz_name = _timezone(env.get("TZ", ""))
    return Config(
        url=url,
        username=username,
        password=password,
        api_key=api_key,
        allow_no_auth=allow_no_auth,
        port=_int(env, "CALDAV_MCP_PORT", 8080, 1, 65535),
        path=path,
        tz=tz,
        tz_name=tz_name,
        week_start=_week_start(env.get("CALDAV_WEEK_START")),
        verify_ssl=_verify_ssl(env.get("CALDAV_MCP_CALDAV_VERIFY_SSL")),
        ca_bundle=env.get("CALDAV_MCP_TLS_CA_BUNDLE", "").strip(),
        tls_cert=tls_cert,
        tls_key=tls_key,
        read_only=_tolerant_on(env.get("CALDAV_MCP_READ_ONLY")),
        allow_tools=_optional_list(env, "CALDAV_ALLOW_TOOLS"),
        deny_tools=_optional_list(env, "CALDAV_DENY_TOOLS"),
        default_calendar=env.get("CALDAV_DEFAULT_CALENDAR", "").strip(),
        max_events=_int(env, "CALDAV_MAX_EVENTS", DEFAULT_MAX_EVENTS, 1, MAX_MAX_EVENTS),
        timeout=_int(env, "CALDAV_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS, 1, 300),
        trust_proxy=_strict_true(env.get("CALDAV_MCP_TRUST_PROXY")),
        rate_limit_max_failures=_int(env, "CALDAV_MCP_RATE_LIMIT_MAX_FAILURES", 10, 1, 1000),
        rate_limit_window_seconds=_int(env, "CALDAV_MCP_RATE_LIMIT_WINDOW_SECONDS", 60, 1, 86400),
    )
