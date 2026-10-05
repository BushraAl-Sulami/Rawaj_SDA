"""Small FastAPI host for Rawaj's real email-response links.

Run this service only when ``PUBLIC_BASE_URL`` points to an address that the
restaurant can actually reach.  It hosts signed button confirmation pages; it
does not pretend to be a dashboard, CRM, payment service, or Strategy Agent.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

try:  # Supports package imports and direct notebook imports.
    from .agent import RawajOutreachApplication
    from .config import ConfigurationError
except ImportError:  # pragma: no cover - direct notebook import support.
    from agent import RawajOutreachApplication
    from config import ConfigurationError


def create_app(application: RawajOutreachApplication | None = None) -> FastAPI:
    """Create the response service with the same runtime used by LangGraph."""

    runtime = application or RawajOutreachApplication.create()
    app = FastAPI(
        title="Rawaj Outreach Response Service",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
    )
    app.state.rawaj_outreach_application = runtime
    app.state.response_endpoint_error = None
    try:
        # The response routes require a configured public URL and signing
        # secret. Keeping the health endpoint available without them makes
        # startup diagnosable, while no button route can accidentally operate
        # in an unconfigured state.
        app.include_router(runtime.response_router())
    except ConfigurationError as error:
        app.state.response_endpoint_error = str(error)

    @app.get("/health")
    def health() -> dict[str, Any]:
        """Return only non-secret diagnostics for local deployment checks."""

        response_status = (
            "ok" if app.state.response_endpoint_error is None else "configuration_required"
        )
        return {
            "service": "rawaj-outreach-response",
            "status": response_status,
            "configuration": runtime.settings.safe_summary(),
            "response_endpoint_ready": app.state.response_endpoint_error is None,
        }

    return app


app = create_app()


__all__ = ["app", "create_app"]
