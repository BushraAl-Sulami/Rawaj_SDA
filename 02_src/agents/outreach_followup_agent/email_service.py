"""Real email preparation, signed response buttons, and provider adapters.

The service never sends a message at import time. LangGraph may call
send_approved_message only after the repository has verified Human Approval for
the exact immutable message version.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import re
import secrets
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen
from .guardrails import credential_delivery_error

try:  # Supports package imports and direct local notebook imports.
    from .config import ConfigurationError, RawajSettings
    from .schemas import (
        ButtonAction,
        EmailButton,
        EmailDraft,
        OutboundMessageType,
        SignedButtonPayload,
    )
except ImportError:  # pragma: no cover - used by a direct notebook import.
    from config import ConfigurationError, RawajSettings
    from schemas import (
        ButtonAction,
        EmailButton,
        EmailDraft,
        OutboundMessageType,
        SignedButtonPayload,
    )


class ButtonTokenError(ValueError):
    """Raised when a signed email-response token is invalid or expired."""


@dataclass(frozen=True)
class PreparedEmail:
    """Final, immutable email content shown to the human approver."""

    draft: EmailDraft
    content_sha256: str


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


class ButtonTokenSigner:
    """Issues and verifies HMAC-signed button tokens without storing secrets."""

    def __init__(self, secret: str) -> None:
        if len(secret) < 32:
            raise ButtonTokenError(
                "Button signing secret must contain at least 32 characters."
            )
        self._secret = secret.encode("utf-8")

    def issue(
        self,
        *,
        restaurant_id: int | str,
        outreach_message_id: str,
        action: ButtonAction,
        expires_in_days: int,
    ) -> tuple[str, SignedButtonPayload]:
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(days=expires_in_days)
        payload = {
            "token_id": secrets.token_urlsafe(24),
            "restaurant_id": str(restaurant_id),
            "outreach_message_id": outreach_message_id,
            "action": action.value,
            "issued_at": int(now.timestamp()),
            "expires_at": int(expires_at.timestamp()),
            "nonce": secrets.token_urlsafe(16),
        }
        encoded_payload = _b64encode(
            json.dumps(
                payload,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )
        signature = hmac.new(
            self._secret,
            encoded_payload.encode("ascii"),
            hashlib.sha256,
        ).digest()
        token = f"{encoded_payload}.{_b64encode(signature)}"
        return token, self._claims_from_payload(payload)

    def verify(self, token: str) -> SignedButtonPayload:
        try:
            encoded_payload, encoded_signature = token.split(".", maxsplit=1)
            expected_signature = hmac.new(
                self._secret,
                encoded_payload.encode("ascii"),
                hashlib.sha256,
            ).digest()
            actual_signature = _b64decode(encoded_signature)
            if not hmac.compare_digest(expected_signature, actual_signature):
                raise ButtonTokenError("Button token signature is invalid.")

            payload = json.loads(_b64decode(encoded_payload))
            claims = self._claims_from_payload(payload)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            if isinstance(error, ButtonTokenError):
                raise
            raise ButtonTokenError("Button token is malformed.") from error

        expires_at = datetime.fromisoformat(claims.expires_at)
        if expires_at <= datetime.now(timezone.utc):
            raise ButtonTokenError("Button token has expired.")
        return claims

    @staticmethod
    def _claims_from_payload(payload: dict[str, Any]) -> SignedButtonPayload:
        action = str(payload.get("action", ""))
        if action not in {ButtonAction.INTERESTED.value, ButtonAction.NOT_INTERESTED.value}:
            raise ButtonTokenError("Button token action is invalid.")

        issued_at = int(payload["issued_at"])
        expires_at = int(payload["expires_at"])
        if expires_at <= issued_at:
            raise ButtonTokenError("Button token expiry is invalid.")

        return SignedButtonPayload(
            token_id=str(payload["token_id"]),
            restaurant_id=str(payload["restaurant_id"]),
            outreach_message_id=str(payload["outreach_message_id"]),
            action=ButtonAction(action),
            issued_at=datetime.fromtimestamp(
                issued_at, tz=timezone.utc
            ).isoformat(),
            expires_at=datetime.fromtimestamp(
                expires_at, tz=timezone.utc
            ).isoformat(),
        )


class EmailService:
    """Prepare approved Rawaj emails and submit them to a real configured provider."""

    RESPONSE_PATH = "/api/outreach/response"

    def __init__(self, settings: RawajSettings) -> None:
        self.settings = settings

    def prepare_for_approval(
        self,
        draft: EmailDraft | dict[str, Any],
    ) -> PreparedEmail:
        """Build final safe HTML and signed buttons before Human Approval.

        The LLM-generated HTML is intentionally not sent directly. The service
        renders the reviewed plain-text body inside a deterministic email shell,
        preventing arbitrary HTML or script content from leaving the system.
        """

        data = (
            draft.model_dump(mode="json")
            if isinstance(draft, EmailDraft)
            else dict(draft)
        )
        message_type = str(data.get("message_type", ""))
        requires_response_buttons = message_type in {
            OutboundMessageType.INITIAL_OUTREACH.value,
            OutboundMessageType.NO_RESPONSE_FOLLOW_UP.value,
        }
        supplied_buttons = data.get("buttons") or []

        if requires_response_buttons:
            buttons = self._reuse_or_build_response_buttons(
                supplied_buttons=supplied_buttons,
                restaurant_id=data.get("restaurant_id"),
                message_id=data.get("message_id"),
            )
        elif supplied_buttons:
            raise ValueError(
                "Only prospect outreach messages may include response buttons."
            )
        else:
            buttons = []

        source_plain_text = self._strip_rendered_response_links(
            str(data.get("plain_text_body", "")), buttons
        )
        data["buttons"] = [button.model_dump(mode="json") for button in buttons]
        data["html_body"] = self._render_html(
            plain_text_body=source_plain_text,
            buttons=buttons,
            message_type=message_type,
        )
        data["plain_text_body"] = self._render_plain_text(
            plain_text_body=source_plain_text,
            buttons=buttons,
        )
        data["status"] = "GENERATED"

        final_draft = EmailDraft.model_validate(data)
        return PreparedEmail(
            draft=final_draft,
            content_sha256=self._content_hash(final_draft),
        )

    def verify_button_token(self, token: str) -> SignedButtonPayload:
        """Verify a token received by the public response endpoint."""

        self.settings.require_button_endpoint()
        signer = ButtonTokenSigner(self.settings.button_signing_secret or "")
        return signer.verify(token)

    def send_approved_message(self, message: dict[str, Any]) -> dict[str, Any]:
        """Submit one repository-claimed message to the configured real provider."""

        if str(message.get("status")) != "SENDING":
            return {
                "success": False,
                "provider": self.settings.email_provider or "NONE",
                "failure_code": "MESSAGE_NOT_CLAIMED",
                "failure_detail": "Message must be claimed by the approval executor first.",
            }

        try:
            self.settings.require_email()
        except ConfigurationError as error:
            return {
                "success": False,
                "provider": self.settings.email_provider or "NONE",
                "failure_code": "EMAIL_NOT_CONFIGURED",
                "failure_detail": str(error),
            }

        try:
            recipient = self._validate_recipient(
                message.get("recipient") or message.get("recipient_email")
            )
            subject = self._validate_header_value(
                message.get("subject"), "subject"
            )
        except ValueError as error:
            return {
                "success": False,
                "provider": self.settings.email_provider or "NONE",
                "failure_code": "INVALID_MESSAGE_HEADER",
                "failure_detail": str(error),
            }

        # Defend again at the irreversible provider boundary in case local
        # development configuration changed after the draft was prepared.
        message_type = str(message.get("message_type") or "")
        if message_type in {
            OutboundMessageType.INITIAL_OUTREACH.value,
            OutboundMessageType.NO_RESPONSE_FOLLOW_UP.value,
        }:
            try:
                self.settings.require_button_endpoint()
            except ConfigurationError as error:
                return {
                    "success": False,
                    "provider": self.settings.email_provider or "NONE",
                    "failure_code": "BUTTON_ENDPOINT_NOT_CONFIGURED",
                    "failure_detail": str(error),
                }

        html_body = str(message.get("html_body") or "").strip()
        plain_text_body = str(message.get("plain_text_body") or "").strip()

        if not subject or not html_body or not plain_text_body:
            return {
                "success": False,
                "provider": self.settings.email_provider or "NONE",
                "failure_code": "INVALID_MESSAGE",
                "failure_detail": "Subject and both email body formats are required.",
            }

        guardrail_error = credential_delivery_error(message)
        if guardrail_error:
            return {"success": False, "provider": self.settings.email_provider,
                    "failure_code": guardrail_error, "failure_detail": "Credential delivery policy blocked this message."}
        if self.settings.email_provider == "resend":
            return self._send_with_resend(
                recipient=recipient,
                subject=subject,
                html_body=html_body,
                plain_text_body=plain_text_body,
            )
        if self.settings.email_provider == "smtp":
            return self._send_with_smtp(
                recipient=recipient,
                subject=subject,
                html_body=html_body,
                plain_text_body=plain_text_body,
            )

        return {
            "success": False,
            "provider": "NONE",
            "failure_code": "EMAIL_NOT_CONFIGURED",
            "failure_detail": "No supported email provider is configured.",
        }

    def _reuse_or_build_response_buttons(
        self,
        *,
        supplied_buttons: list[EmailButton | dict[str, Any]],
        restaurant_id: int | str | None,
        message_id: str | None,
    ) -> list[EmailButton]:
        """Reuse a persisted final version, or create its first signed version.

        LangGraph must keep the returned final draft in state before review and
        approval. Reusing it on a retry keeps the token URLs and content hash
        immutable instead of silently creating a new approval target.
        """

        if not supplied_buttons:
            return self._build_response_buttons(
                restaurant_id=restaurant_id,
                message_id=message_id,
            )

        try:
            buttons = [
                button
                if isinstance(button, EmailButton)
                else EmailButton.model_validate(button)
                for button in supplied_buttons
            ]
        except (TypeError, ValueError) as error:
            raise ValueError("Persisted response buttons are invalid.") from error

        expected_actions = {ButtonAction.INTERESTED, ButtonAction.NOT_INTERESTED}
        if len(buttons) != 2 or {button.action for button in buttons} != expected_actions:
            raise ValueError(
                "Persisted response buttons must include exactly Interested and "
                "Not Interested."
            )

        expected_base = urlparse((self.settings.public_base_url or "").rstrip("/"))
        for button in buttons:
            parsed = urlparse(button.url)
            if (
                parsed.scheme != expected_base.scheme
                or parsed.netloc != expected_base.netloc
                or parsed.path != self.RESPONSE_PATH
            ):
                raise ValueError("Persisted response button URL is not a Rawaj URL.")
            token = parse_qs(parsed.query).get("token", [None])[0]
            if not token:
                raise ValueError("Persisted response button is missing its token.")
            claims = self.verify_button_token(token)
            if (
                str(claims.restaurant_id) != str(restaurant_id)
                or claims.outreach_message_id != str(message_id)
                or claims.action is not button.action
            ):
                raise ValueError(
                    "Persisted response button does not belong to this message."
                )

        return buttons

    def _build_response_buttons(
        self,
        *,
        restaurant_id: int | str | None,
        message_id: str | None,
    ) -> list[EmailButton]:
        self.settings.require_button_endpoint()

        if restaurant_id is None:
            raise ValueError("restaurant_id is required for response buttons.")
        if not message_id:
            raise ValueError("message_id is required for response buttons.")

        signer = ButtonTokenSigner(self.settings.button_signing_secret or "")
        labels = {
            ButtonAction.INTERESTED: "Yes, I'm interested",
            ButtonAction.NOT_INTERESTED: "No, thank you",
        }
        buttons: list[EmailButton] = []

        for action in (ButtonAction.INTERESTED, ButtonAction.NOT_INTERESTED):
            token, _ = signer.issue(
                restaurant_id=restaurant_id,
                outreach_message_id=message_id,
                action=action,
                expires_in_days=self.settings.button_response_ttl_days,
            )
            query = urlencode({"token": token})
            base_url = (self.settings.public_base_url or "").rstrip("/")
            buttons.append(
                EmailButton(
                    action=action,
                    label=labels[action],
                    url=f"{base_url}{self.RESPONSE_PATH}?{query}",
                )
            )

        return buttons

    @staticmethod
    def _render_html(
        *,
        plain_text_body: str,
        buttons: list[EmailButton],
        message_type: str = "",
    ) -> str:
        escaped_body = html.escape(plain_text_body.strip()).replace("\n", "<br>\n")
        if message_type == OutboundMessageType.STRATEGY_READY_NOTIFICATION.value:
            # Add only trusted emphasis after escaping the model's body.
            # Keep the reviewed text and all surrounding words unchanged.
            escaped_body = re.sub(
                r"\b30-day free trial\b",
                lambda match: f"<strong>{match.group(0)}</strong>",
                escaped_body,
                flags=re.IGNORECASE,
            )
        button_html = ""

        if buttons:
            rendered_buttons = []
            for button in buttons:
                background = (
                    "#1D6F42"
                    if button.action is ButtonAction.INTERESTED
                    else "#6B7280"
                )
                rendered_buttons.append(
                    (
                        '<a href="{url}" style="display:inline-block;'
                        'margin:8px 8px 0 0;padding:12px 18px;border-radius:6px;'
                        'background:{background};color:#ffffff;text-decoration:none;'
                        'font-family:Arial,sans-serif;font-weight:600;">{label}</a>'
                    ).format(
                        url=html.escape(button.url, quote=True),
                        background=background,
                        label=html.escape(button.label),
                    )
                )
            button_html = (
                '<div style="margin-top:20px;">'
                + "".join(rendered_buttons)
                + "</div>"
            )

        return (
            '<!doctype html><html><body style="margin:0;padding:0;'
            'background:#f7f7f7;"><div style="max-width:640px;margin:24px auto;'
            'padding:28px;background:#ffffff;color:#1f2937;font-family:Arial,'
            'sans-serif;font-size:16px;line-height:1.6;border-radius:10px;">'
            + f'<div style="font-weight:700;color:#1D6F42;margin-bottom:18px;">Rawaj</div>'
            + f"<div>{escaped_body}</div>"
            + button_html
            + '<div style="margin-top:24px;color:#4b5563;">Rawaj Team</div>'
            + "</div></body></html>"
        )

    @staticmethod
    def _render_plain_text(
        *,
        plain_text_body: str,
        buttons: list[EmailButton],
    ) -> str:
        body = plain_text_body.strip()
        if not buttons:
            return body

        links = "\n\n".join(
            f"{button.label}: {button.url}" for button in buttons
        )
        return f"{body}\n\n{links}"

    @staticmethod
    def _strip_rendered_response_links(
        plain_text_body: str,
        buttons: list[EmailButton],
    ) -> str:
        """Recover the reviewed body when a prepared draft is resumed.

        The trusted renderer adds the visible fallback links for mail clients
        that cannot render buttons. A graph retry receives that final draft, so
        removing only this exact suffix prevents duplicate links and preserves
        the same immutable content hash.
        """

        if not buttons:
            return plain_text_body.strip()

        rendered_links = "\n\n".join(
            f"{button.label}: {button.url}" for button in buttons
        )
        suffix = f"\n\n{rendered_links}"
        body = plain_text_body.strip()
        return body[: -len(suffix)] if body.endswith(suffix) else body

    @staticmethod
    def _content_hash(draft: EmailDraft) -> str:
        canonical = {
            "recipient": draft.recipient,
            "subject": draft.subject,
            "html_body": draft.html_body,
            "plain_text_body": draft.plain_text_body,
            "buttons": [
                button.model_dump(mode="json") for button in draft.buttons
            ],
            "message_type": draft.message_type.value,
            "action": draft.action.value,
            "language": draft.language,
        }
        serialized = json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def _from_header(self) -> str:
        from_name = self._validate_header_value(self.settings.from_name, "FROM_NAME")
        from_email = self._validate_recipient(self.settings.from_email)
        return formataddr(
            (
                from_name,
                from_email,
            )
        )

    @staticmethod
    def _validate_header_value(value: Any, field_name: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError(f"{field_name} is required.")
        if "\r" in text or "\n" in text:
            raise ValueError(f"{field_name} must not contain line breaks.")
        return text

    @classmethod
    def _validate_recipient(cls, value: Any) -> str:
        text = cls._validate_header_value(value, "recipient email")
        _, parsed_address = parseaddr(text)
        if not parsed_address or parsed_address != text or text.count("@") != 1:
            raise ValueError("A single valid recipient email address is required.")
        local_part, _, domain = text.partition("@")
        if not local_part or not domain or "." not in domain:
            raise ValueError("A single valid recipient email address is required.")
        return text

    def _send_with_resend(
        self,
        *,
        recipient: str,
        subject: str,
        html_body: str,
        plain_text_body: str,
    ) -> dict[str, Any]:
        payload = {
            "from": self._from_header(),
            "to": [recipient],
            "subject": subject,
            "html": html_body,
            "text": plain_text_body,
        }
        request = Request(
            "https://api.resend.com/emails",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.settings.resend_api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urlopen(request, timeout=20) as response:
                response_data = json.loads(response.read().decode("utf-8"))
                provider_message_id = response_data.get("id")
                if not provider_message_id:
                    return {
                        "success": False,
                        "provider": "resend",
                        "failure_code": "MISSING_PROVIDER_MESSAGE_ID",
                        "failure_detail": "Resend accepted the request without an email ID.",
                    }
                return {
                    "success": True,
                    "provider": "resend",
                    "provider_message_id": str(provider_message_id),
                    "provider_status": "ACCEPTED_BY_RESEND",
                }
        except HTTPError as error:
            return {
                "success": False,
                "provider": "resend",
                "failure_code": f"HTTP_{error.code}",
                "failure_detail": "Resend rejected the email request.",
            }
        except URLError:
            return {
                "success": False,
                "provider": "resend",
                "failure_code": "NETWORK_ERROR",
                "failure_detail": "Could not reach Resend.",
            }
        except (TimeoutError, json.JSONDecodeError):
            return {
                "success": False,
                "provider": "resend",
                "failure_code": "PROVIDER_RESPONSE_ERROR",
                "failure_detail": "Resend did not return a usable response.",
            }

    def _send_with_smtp(
        self,
        *,
        recipient: str,
        subject: str,
        html_body: str,
        plain_text_body: str,
    ) -> dict[str, Any]:
        message = EmailMessage()
        message["From"] = self._from_header()
        message["To"] = recipient
        message["Subject"] = subject
        message["Message-ID"] = make_msgid()
        message.set_content(plain_text_body)
        message.add_alternative(html_body, subtype="html")

        try:
            with smtplib.SMTP(
                self.settings.smtp_host,
                self.settings.smtp_port,
                timeout=20,
            ) as server:
                server.ehlo()
                if self.settings.smtp_use_tls:
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
                server.login(
                    self.settings.smtp_username,
                    self.settings.smtp_password,
                )
                server.send_message(message)

            return {
                "success": True,
                "provider": "smtp",
                "provider_message_id": str(message["Message-ID"]),
                "provider_status": "SUBMITTED_TO_SMTP",
            }
        except smtplib.SMTPException:
            return {
                "success": False,
                "provider": "smtp",
                "failure_code": "SMTP_ERROR",
                "failure_detail": "SMTP did not accept the email.",
            }
        except OSError:
            return {
                "success": False,
                "provider": "smtp",
                "failure_code": "NETWORK_ERROR",
                "failure_detail": "Could not reach the SMTP server.",
            }


__all__ = [
    "ButtonTokenError",
    "ButtonTokenSigner",
    "EmailService",
    "PreparedEmail",
]
