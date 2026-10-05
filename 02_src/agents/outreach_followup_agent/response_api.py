"""Scanner-safe public response endpoint for Rawaj outreach email buttons.

The button URL opens a confirmation page with GET.  GET never changes state,
so mail-security scanners cannot accidentally mark a restaurant Interested or
Not Interested.  Only the subsequent signed POST records a response event.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

try:  # Supports package imports and direct local notebook imports.
    from .email_service import ButtonTokenError, EmailService
    from .schemas import ButtonAction, ButtonClickEvent
except ImportError:  # pragma: no cover - used by a direct notebook import.
    from email_service import ButtonTokenError, EmailService
    from schemas import ButtonAction, ButtonClickEvent


class ConfirmationTokenError(ValueError):
    """Raised when the server-side confirmation form token is invalid."""


@dataclass(frozen=True)
class ConfirmationClaims:
    """Short-lived claims authorizing a POST for one opened button page."""

    confirmation_id: str
    email_token_sha256: str
    expires_at: str


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + ("=" * (-len(value) % 4)))


class ConfirmationTokenSigner:
    """Sign a short-lived confirmation token without storing browser sessions."""

    def __init__(self, secret: str) -> None:
        if len(secret) < 32:
            raise ConfirmationTokenError(
                "Button signing secret must contain at least 32 characters."
            )
        self._secret = secret.encode("utf-8")

    def issue(
        self,
        email_token: str,
        *,
        confirmation_id: str,
        expires_in_minutes: int = 15,
    ) -> tuple[str, ConfirmationClaims]:
        if expires_in_minutes <= 0:
            raise ConfirmationTokenError("Confirmation expiry must be positive.")
        if not confirmation_id.strip():
            raise ConfirmationTokenError("Confirmation ID is required.")
        expires_at = datetime.now(timezone.utc) + timedelta(
            minutes=expires_in_minutes
        )
        payload = {
            "confirmation_id": confirmation_id,
            "email_token_sha256": hashlib.sha256(
                email_token.encode("utf-8")
            ).hexdigest(),
            "expires_at": int(expires_at.timestamp()),
            "nonce": secrets.token_urlsafe(18),
        }
        encoded_payload = _b64encode(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        )
        signature = hmac.new(
            self._secret,
            encoded_payload.encode("ascii"),
            hashlib.sha256,
        ).digest()
        claims = ConfirmationClaims(
            confirmation_id=confirmation_id,
            email_token_sha256=payload["email_token_sha256"],
            expires_at=expires_at.isoformat(),
        )
        return f"{encoded_payload}.{_b64encode(signature)}", claims

    def verify(self, confirmation_token: str, email_token: str) -> ConfirmationClaims:
        try:
            encoded_payload, encoded_signature = confirmation_token.split(
                ".", maxsplit=1
            )
            expected_signature = hmac.new(
                self._secret,
                encoded_payload.encode("ascii"),
                hashlib.sha256,
            ).digest()
            if not hmac.compare_digest(
                expected_signature, _b64decode(encoded_signature)
            ):
                raise ConfirmationTokenError("Confirmation token signature is invalid.")
            payload = json.loads(_b64decode(encoded_payload))
            expected_hash = hashlib.sha256(email_token.encode("utf-8")).hexdigest()
            if not hmac.compare_digest(
                str(payload["email_token_sha256"]), expected_hash
            ):
                raise ConfirmationTokenError(
                    "Confirmation token does not match this response link."
                )
            expires_at = int(payload["expires_at"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            if isinstance(error, ConfirmationTokenError):
                raise
            raise ConfirmationTokenError("Confirmation token is malformed.") from error

        if expires_at <= int(datetime.now(timezone.utc).timestamp()):
            raise ConfirmationTokenError("Confirmation token has expired.")
        return ConfirmationClaims(
            confirmation_id=str(payload["confirmation_id"]),
            email_token_sha256=expected_hash,
            expires_at=datetime.fromtimestamp(
                expires_at, tz=timezone.utc
            ).isoformat(),
        )


def create_response_router(
    *,
    email_service: EmailService,
    repository: Any,
    workflow_dispatcher: Any | None = None,
) -> APIRouter:
    """Create the public router wired to real email and persistence services.

    ``repository`` must provide ``record_button_event``.  If a real LangGraph
    dispatcher is supplied, it must provide ``resume_from_button_event``; it is
    called only after the signed event was committed.  A missing dispatcher
    records the truthful pending event instead of faking Strategy generation.
    """

    settings = email_service.settings
    settings.require_button_endpoint()
    confirmation_signer = ConfirmationTokenSigner(
        settings.button_signing_secret or ""
    )
    router = APIRouter(prefix="/api/outreach", tags=["outreach-response"])

    @router.get("/response", response_class=HTMLResponse)
    def show_confirmation(token: str, request: Request) -> HTMLResponse:
        """Render confirmation only. Never mutate relationship state on GET."""

        try:
            claims = email_service.verify_button_token(token)
            confirmation, confirmation_claims = confirmation_signer.issue(
                token,
                confirmation_id=f"confirm_{secrets.token_urlsafe(24)}",
            )
            repository.create_button_confirmation(
                confirmation_id=confirmation_claims.confirmation_id,
                email_token_sha256=confirmation_claims.email_token_sha256,
                outreach_message_id=claims.outreach_message_id,
                expires_at=confirmation_claims.expires_at,
            )
        except (ButtonTokenError, ConfirmationTokenError, ValueError):
            return _page(
                title="This response link is unavailable",
                body="This response link is invalid or has expired. Please contact Rawaj if you need help.",
                status_code=400,
            )
        except Exception:
            return _page(
                title="This response page is temporarily unavailable",
                body="Please try again shortly or contact Rawaj directly.",
                status_code=503,
            )

        is_interested = claims.action is ButtonAction.INTERESTED
        action_text = (
            "tell Rawaj that you are interested"
            if is_interested
            else "stop promotional contact from Rawaj"
        )
        button_label = "Yes, I’m interested" if is_interested else "Yes, stop contact"
        return _confirmation_page(
            action_text=action_text,
            button_label=button_label,
            token=token,
            confirmation=confirmation,
            post_action=request.url.path,
        )

    @router.post("/response", response_class=HTMLResponse)
    async def confirm_response(request: Request) -> HTMLResponse:
        """Verify an explicit POST, persist one response, then resume the graph."""

        try:
            form_values = _read_urlencoded_form(await request.body())
            token = form_values["token"]
            confirmation = form_values["confirmation"]
            confirmation_claims = confirmation_signer.verify(confirmation, token)
            claims = email_service.verify_button_token(token)
        except (ButtonTokenError, ConfirmationTokenError, ValueError):
            return _page(
                title="This response could not be confirmed",
                body="Please reopen the response link from the Rawaj email and try again.",
                status_code=400,
            )

        try:
            consumed = repository.consume_button_confirmation(
                confirmation_id=confirmation_claims.confirmation_id,
                email_token_sha256=confirmation_claims.email_token_sha256,
                outreach_message_id=claims.outreach_message_id,
            )
        except Exception:
            return _page(
                title="We could not confirm your response",
                body="Please try again shortly or contact Rawaj directly.",
                status_code=503,
            )
        if not consumed.get("consumed"):
            return _page(
                title="This confirmation is no longer available",
                body="Please reopen the response link from the Rawaj email and try again.",
                status_code=409,
            )

        event = ButtonClickEvent(
            restaurant_id=claims.restaurant_id,
            outreach_message_id=claims.outreach_message_id,
            action=claims.action,
            token_id=claims.token_id,
            idempotency_key=f"email-button:{claims.token_id}",
            source_ip_hash=_source_ip_hash(request, settings.button_signing_secret or ""),
        )
        try:
            recorded = repository.record_button_event(event=event)
        except ValueError:
            return _page(
                title="This response link is unavailable",
                body="This response cannot be recorded. Please contact Rawaj if you need help.",
                status_code=409,
            )
        except Exception:
            return _page(
                title="We could not record your response",
                body="Please try again shortly or contact Rawaj directly.",
                status_code=503,
            )

        status = str(recorded.get("status") or "")
        if status == "DUPLICATE":
            return _page(
                title="Your response is already recorded",
                body="Thank you. Rawaj already has your response.",
            )
        if status in {"DO_NOT_CONTACT_APPLIED", "DO_NOT_CONTACT_ALREADY_SET"}:
            return _page(
                title="Your preference is recorded",
                body="Rawaj will not continue promotional contact.",
            )
        if not recorded.get("accepted"):
            return _page(
                title="This response could not be recorded",
                body="Please contact Rawaj if you need help.",
                status_code=409,
            )

        dispatch_warning = False
        if workflow_dispatcher is not None and recorded.get("event"):
            try:
                result = workflow_dispatcher.resume_from_button_event(recorded["event"])
                dispatch_warning = bool(getattr(result, "errors", []))
            except Exception:
                # The event is already durable. Do not claim the next workflow
                # action happened when its dispatcher was unavailable.
                dispatch_warning = True

        if claims.action is ButtonAction.INTERESTED:
            body = "Thank you — Rawaj has recorded your interest."
        else:
            body = "Thank you — Rawaj has recorded your preference and will stop promotional contact."
        if dispatch_warning and claims.action is ButtonAction.INTERESTED:
            body += " The next step is awaiting Rawaj's workflow."
        return _page(title="Response recorded", body=body)

    return router


def _source_ip_hash(request: Request, secret: str) -> str | None:
    """Store a salted digest for abuse diagnostics, never a raw IP address."""

    client = request.client
    if client is None or not client.host or not secret:
        return None
    return hmac.new(
        secret.encode("utf-8"),
        client.host.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _read_urlencoded_form(body: bytes) -> dict[str, str]:
    """Parse the small signed confirmation form without a multipart dependency."""

    if len(body) > 16_384:
        raise ValueError("Confirmation form is too large.")
    try:
        parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True)
    except UnicodeDecodeError as error:
        raise ValueError("Confirmation form is invalid.") from error

    result: dict[str, str] = {}
    for key in ("token", "confirmation"):
        values = parsed.get(key, [])
        if len(values) != 1 or not values[0]:
            raise ValueError(f"Confirmation form is missing {key}.")
        result[key] = values[0]
    return result


def _confirmation_page(
    *,
    action_text: str,
    button_label: str,
    token: str,
    confirmation: str,
    post_action: str,
) -> HTMLResponse:
    return HTMLResponse(
        content=f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Confirm your Rawaj response</title></head>
<body style="font-family:Arial,sans-serif;max-width:620px;margin:48px auto;padding:24px;color:#1f2937;">
  <h1 style="color:#1D6F42;">Rawaj</h1>
  <p>Please confirm that you want to {html.escape(action_text)}.</p>
  <form method="post" action="{html.escape(post_action, quote=True)}">
    <input type="hidden" name="token" value="{html.escape(token, quote=True)}">
    <input type="hidden" name="confirmation" value="{html.escape(confirmation, quote=True)}">
    <button type="submit" style="background:#1D6F42;color:white;border:0;border-radius:6px;padding:12px 18px;font-size:16px;cursor:pointer;">
      {html.escape(button_label)}
    </button>
  </form>
</body></html>""",
        status_code=200,
        headers=_security_headers(),
    )


def _page(*, title: str, body: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(
        content=f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{html.escape(title)}</title></head>
<body style="font-family:Arial,sans-serif;max-width:620px;margin:48px auto;padding:24px;color:#1f2937;">
  <h1 style="color:#1D6F42;">{html.escape(title)}</h1>
  <p>{html.escape(body)}</p>
</body></html>""",
        status_code=status_code,
        headers=_security_headers(),
    )


def _security_headers() -> dict[str, str]:
    """Prevent caching, referrer leakage, and framed confirmation clicks."""

    return {
        "Cache-Control": "no-store, max-age=0",
        "Pragma": "no-cache",
        "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "DENY",
        "Content-Security-Policy": (
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
            "frame-ancestors 'none'; base-uri 'none'"
        ),
    }


__all__ = [
    "ConfirmationClaims",
    "ConfirmationTokenError",
    "ConfirmationTokenSigner",
    "create_response_router",
]
