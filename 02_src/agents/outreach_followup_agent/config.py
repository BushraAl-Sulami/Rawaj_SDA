"""Environment-backed configuration for Rawaj's Outreach & Follow-Up Agent.

Secrets stay in a local .env file. This module only reads and validates them;
it never writes credentials, sends requests, or includes a payment system.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv


class ConfigurationError(ValueError):
    """Raised when a real integration is used without its required settings."""


# The notebook is opened from ``agents/outreach_followup_agent`` while the
# application can also run from the repository root.  Runtime files must not
# change location merely because the current working directory changes.
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _clean(name: str) -> str | None:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else None


def _positive_int(name: str, default: int) -> int:
    raw = _clean(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as error:
        raise ConfigurationError(f"{name} must be an integer.") from error
    if value <= 0:
        raise ConfigurationError(f"{name} must be greater than zero.")
    return value


def _as_bool(name: str, default: bool = False) -> bool:
    raw = _clean(name)
    if raw is None:
        return default
    normalized = raw.lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false.")


def _require(value: str | None, name: str) -> str:
    if not value:
        raise ConfigurationError(f"{name} is required for this integration.")
    return value


def _project_path(value: str) -> str:
    """Resolve a local runtime path against the repository, not the notebook."""

    path = Path(value).expanduser()
    return str(path if path.is_absolute() else PROJECT_ROOT / path)


def _validate_url(value: str, name: str, *, require_https: bool) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigurationError(f"{name} must be an absolute http(s) URL.")
    if require_https and parsed.scheme != "https":
        raise ConfigurationError(f"{name} must use HTTPS in production.")
    return value.rstrip("/")


@dataclass(frozen=True)
class RawajSettings:
    """Configuration loaded from environment variables.

    A missing key does not fail at import time. The corresponding require_*
    method fails only when that real integration is about to be used.
    """

    environment: str = "development"
    database_url: str = "sqlite:///rawaj.db"
    langgraph_checkpoint_path: str = "rawaj_outreach_checkpoints.sqlite"

    openai_api_key: str | None = field(default=None, repr=False)
    openai_generation_model: str = "gpt-5.6-luna"
    openai_decision_model: str = "gpt-5.6-luna"
    openai_review_model: str = "gpt-5.6-luna"

    email_provider: str | None = None
    from_email: str | None = None
    from_name: str = "Rawaj Team"
    resend_api_key: str | None = field(default=None, repr=False)
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = field(default=None, repr=False)
    smtp_use_tls: bool = True
    email_webhook_secret: str | None = field(default=None, repr=False)

    public_base_url: str | None = None
    button_signing_secret: str | None = field(default=None, repr=False)
    button_response_ttl_days: int = 30
    allow_local_button_demo: bool = False
    strategy_handoff_outbox: str = "strategy_handoffs/inbox"

    rawaj_events_calendar_id: str | None = None
    google_oauth_client_secret_path: str | None = None
    google_oauth_token_path: str = "secrets/google_token.json"
    google_service_account_file: str | None = None
    google_calendar_write_enabled: bool = False

    follow_up_delay_minutes: int | None = None
    follow_up_max_attempts: int = 3
    client_follow_up_delay_minutes: int = 10_080
    calendar_lookahead_days: int = 45
    approval_ttl_minutes: int = 120

    def __post_init__(self):
        if self.follow_up_delay_minutes is None:
            object.__setattr__(self, "follow_up_delay_minutes", 10080 if self.is_production else 2)

    @classmethod
    def from_environment(
        cls, dotenv_path: str | Path | None = None
    ) -> "RawajSettings":
        """Load optional local .env values without overriding shell variables."""

        load_dotenv(
            dotenv_path=dotenv_path or (PROJECT_ROOT / ".env"),
            override=False,
        )

        provider = (_clean("EMAIL_PROVIDER") or "").lower() or None
        if provider not in {None, "resend", "smtp"}:
            raise ConfigurationError(
                "EMAIL_PROVIDER must be either 'resend' or 'smtp'."
            )

        return cls(
            environment=(_clean("RAWAJ_ENV") or "development").lower(),
            database_url=_clean("DATABASE_URL") or "sqlite:///rawaj.db",
            langgraph_checkpoint_path=_project_path(
                _clean("LANGGRAPH_CHECKPOINT_PATH")
                or "rawaj_outreach_checkpoints.sqlite"
            ),
            openai_api_key=_clean("OPENAI_API_KEY"),
            openai_generation_model=(
                _clean("OPENAI_GENERATION_MODEL") or _clean("OPENAI_MODEL") or "gpt-5.6-luna"
            ),
            openai_decision_model=(
                _clean("OPENAI_DECISION_MODEL") or _clean("OPENAI_MODEL") or "gpt-5.6-luna"
            ),
            openai_review_model=(
                _clean("OPENAI_REVIEW_MODEL") or _clean("OPENAI_MODEL") or "gpt-5.6-luna"
            ),
            email_provider=provider,
            from_email=_clean("FROM_EMAIL"),
            from_name=_clean("FROM_NAME") or "Rawaj Team",
            resend_api_key=_clean("RESEND_API_KEY"),
            smtp_host=_clean("SMTP_HOST"),
            smtp_port=_positive_int("SMTP_PORT", 587),
            smtp_username=_clean("SMTP_USERNAME"),
            smtp_password=_clean("SMTP_PASSWORD"),
            smtp_use_tls=_as_bool("SMTP_USE_TLS", True),
            email_webhook_secret=_clean("EMAIL_WEBHOOK_SECRET"),
            public_base_url=_clean("PUBLIC_BASE_URL"),
            button_signing_secret=_clean("BUTTON_SIGNING_SECRET"),
            button_response_ttl_days=_positive_int(
                "BUTTON_RESPONSE_TTL_DAYS", 30
            ),
            allow_local_button_demo=_as_bool("ALLOW_LOCAL_BUTTON_DEMO", False),
            strategy_handoff_outbox=_project_path(
                _clean("STRATEGY_HANDOFF_OUTBOX")
                or "strategy_handoffs/inbox"
            ),
            rawaj_events_calendar_id=_clean("RAWAJ_EVENTS_CALENDAR_ID"),
            google_oauth_client_secret_path=_clean(
                "GOOGLE_OAUTH_CLIENT_SECRET_PATH"
            ),
            google_oauth_token_path=(
                _clean("GOOGLE_OAUTH_TOKEN_PATH")
                or "secrets/google_token.json"
            ),
            google_service_account_file=_clean("GOOGLE_SERVICE_ACCOUNT_FILE"),
            google_calendar_write_enabled=_as_bool(
                "GOOGLE_CALENDAR_WRITE_ENABLED", False
            ),
            follow_up_delay_minutes=_positive_int(
                "FOLLOW_UP_DELAY_MINUTES", 2 if (_clean("RAWAJ_ENV") or "development").lower() not in {"production", "prod"} else 10_080
            ),
            follow_up_max_attempts=_positive_int(
                "FOLLOW_UP_MAX_ATTEMPTS", 3
            ),
            client_follow_up_delay_minutes=_positive_int(
                "CLIENT_FOLLOW_UP_DELAY_MINUTES", 10_080
            ),
            calendar_lookahead_days=_positive_int(
                "CALENDAR_LOOKAHEAD_DAYS", 45
            ),
            approval_ttl_minutes=_positive_int("APPROVAL_TTL_MINUTES", 120),
        )

    @property
    def is_production(self) -> bool:
        return self.environment in {"production", "prod"}

    def require_openai(self) -> None:
        """Validate the real ChatOpenAI integration just before use."""

        _require(self.openai_api_key, "OPENAI_API_KEY")

    def require_button_endpoint(self) -> None:
        """Validate the public signed-link endpoint used by real email buttons."""

        base_url = _require(self.public_base_url, "PUBLIC_BASE_URL")
        _validate_url(
            base_url,
            "PUBLIC_BASE_URL",
            require_https=self.is_production,
        )

        host = (urlparse(base_url).hostname or "").lower()
        is_loopback = host in {"localhost", "127.0.0.1", "::1"}
        if is_loopback and self.is_production:
            raise ConfigurationError(
                "PUBLIC_BASE_URL must not use a loopback address in production."
            )
        if is_loopback and not self.allow_local_button_demo:
            raise ConfigurationError(
                "Loopback response buttons are for a controlled local demo only. "
                "Set ALLOW_LOCAL_BUTTON_DEMO=true in development/test, or use a "
                "public HTTPS URL."
            )

        secret = _require(self.button_signing_secret, "BUTTON_SIGNING_SECRET")
        if len(secret) < 32:
            raise ConfigurationError(
                "BUTTON_SIGNING_SECRET must contain at least 32 characters."
            )

    def require_email(self) -> None:
        """Validate a chosen real email provider before an outbound send."""

        provider = _require(self.email_provider, "EMAIL_PROVIDER")
        from_email = _require(self.from_email, "FROM_EMAIL")
        if "@" not in from_email:
            raise ConfigurationError("FROM_EMAIL must be a valid email address.")

        if provider == "resend":
            _require(self.resend_api_key, "RESEND_API_KEY")
            return

        if provider == "smtp":
            _require(self.smtp_host, "SMTP_HOST")
            _require(self.smtp_username, "SMTP_USERNAME")
            _require(self.smtp_password, "SMTP_PASSWORD")
            return

        raise ConfigurationError("EMAIL_PROVIDER must be either 'resend' or 'smtp'.")

    def require_calendar_read(self) -> None:
        """Validate real Rawaj Events Calendar read access before a query."""

        _require(self.rawaj_events_calendar_id, "RAWAJ_EVENTS_CALENDAR_ID")
        if not (
            self.google_service_account_file
            or self.google_oauth_client_secret_path
        ):
            raise ConfigurationError(
                "Set GOOGLE_SERVICE_ACCOUNT_FILE or "
                "GOOGLE_OAUTH_CLIENT_SECRET_PATH for Google Calendar."
            )

    def require_calendar_write(self) -> None:
        """Calendar writes are disabled unless explicitly enabled and configured."""

        if not self.google_calendar_write_enabled:
            raise ConfigurationError(
                "GOOGLE_CALENDAR_WRITE_ENABLED is false; Calendar writing is disabled."
            )
        self.require_calendar_read()

    def safe_summary(self) -> dict[str, object]:
        """Return non-secret diagnostics that are safe to display in a notebook."""

        return {
            "environment": self.environment,
            "database_url": self.database_url,
            "langgraph_checkpoint_path": self.langgraph_checkpoint_path,
            "openai_configured": bool(self.openai_api_key),
            "generation_model": self.openai_generation_model,
            "decision_model": self.openai_decision_model,
            "review_model": self.openai_review_model,
            "email_provider": self.email_provider,
            "email_configured": bool(self.email_provider and self.from_email),
            "calendar_configured": bool(
                self.rawaj_events_calendar_id
                and (
                    self.google_service_account_file
                    or self.google_oauth_client_secret_path
                )
            ),
            "button_endpoint_configured": bool(
                self.public_base_url and self.button_signing_secret
            ),
            "button_response_ttl_days": self.button_response_ttl_days,
            "allow_local_button_demo": self.allow_local_button_demo,
            "strategy_handoff_outbox": self.strategy_handoff_outbox,
            "follow_up_delay_minutes": self.follow_up_delay_minutes,
            "follow_up_max_attempts": self.follow_up_max_attempts,
            "client_follow_up_delay_minutes": self.client_follow_up_delay_minutes,
        }


@lru_cache(maxsize=1)
def get_settings() -> RawajSettings:
    """Return cached local settings for application and notebook use."""

    return RawajSettings.from_environment()


def reload_settings() -> RawajSettings:
    """Reload settings after a notebook user changes their local .env file."""

    get_settings.cache_clear()
    return get_settings()


__all__ = [
    "ConfigurationError",
    "RawajSettings",
    "get_settings",
    "reload_settings",
]
