"""Real Google Calendar access for the Rawaj Events calendar.

Calendar is optional decision context, never a required outreach step.  This
service reads verified events only when the workflow has a genuine occasion or
seasonal reason to check them.  It never invents an event.  Its write method is
separate, disabled by default, and requires explicit human approval.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

try:  # Supports package imports and direct local notebook imports.
    from .config import RawajSettings
    from .schemas import CalendarEvent
except ImportError:  # pragma: no cover - used by a direct notebook import.
    from config import RawajSettings
    from schemas import CalendarEvent


class CalendarIntegrationError(RuntimeError):
    """Raised when the configured Google Calendar integration cannot run."""


GoogleServiceFactory = Callable[[bool], Any]
CalendarWriteGuard = Any


class CalendarService:
    """Read relevant facts from Rawaj's configured Google Calendar.

    The service authenticates lazily: importing the agent or using an unrelated
    workflow path never opens a browser, reads credentials, or calls Google.
    ``service_factory`` exists for deterministic tests; production leaves it
    unset and uses Google's official client libraries.
    """

    RIYADH = ZoneInfo("Asia/Riyadh")
    READ_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
    WRITE_SCOPE = "https://www.googleapis.com/auth/calendar.events"

    # These are broad, public restaurant-marketing occasions.  An event outside
    # this set is not returned merely because it happens to be on the calendar.
    OCCASION_KEYWORDS = {
        "ramadan",
        "رمضان",
        "eid",
        "عيد",
        "hajj",
        "الحج",
        "national day",
        "اليوم الوطني",
        "founding day",
        "يوم التأسيس",
        "saudi",
        "السعودي",
        "summer",
        "صيف",
        "winter",
        "شتاء",
        "spring",
        "ربيع",
        "autumn",
        "خريف",
        "coffee",
        "قهوة",
        "food",
        "طعام",
        "tourism",
        "tourist",
        "سياحة",
        "festival",
        "مهرجان",
        "world day",
        "international day",
        "mother's day",
        "valentine",
        "new year",
        "back to school",
        "weekend",
    }

    def __init__(
        self,
        settings: RawajSettings,
        *,
        service_factory: GoogleServiceFactory | None = None,
        calendar_write_guard: CalendarWriteGuard | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self._service_factory = service_factory
        # A write guard is a persistence-backed capability checker.  It must
        # atomically verify an exact human approval and reserve its idempotency
        # key before a Google write can be attempted.
        self._calendar_write_guard = calendar_write_guard
        self._now_provider = now_provider or (lambda: datetime.now(self.RIYADH))

    def get_relevant_events(
        self,
        *,
        restaurant_id: int | str,
        start_at: datetime,
        end_at: datetime,
        restaurant_context: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Return verified, potentially relevant Rawaj Calendar events.

        Results remain optional context.  The email generation node may mention
        an event only when it makes natural sense for the restaurant; it must
        never imply that the event is a commitment, discount, or approved plan.
        ``restaurant_id`` is accepted for audit-friendly tool calls but is not
        sent to Google.
        """

        del restaurant_id  # The calendar query is scoped to Rawaj, not a CRM.
        self.settings.require_calendar_read()
        start = self._as_riyadh_time(start_at)
        end = self._as_riyadh_time(end_at)
        if end <= start:
            raise ValueError("Calendar end_at must be later than start_at.")
        self._validate_read_window(start=start, end=end)

        client = self._calendar_client(write=False)
        raw_events = self._list_events(
            client=client,
            calendar_id=self.settings.rawaj_events_calendar_id or "",
            start=start,
            end=end,
        )

        events: list[dict[str, Any]] = []
        for raw_event in raw_events:
            if not self._is_marketing_event_candidate(raw_event, restaurant_context):
                continue
            event = self._to_calendar_event(
                raw_event,
                calendar_id=self.settings.rawaj_events_calendar_id or "",
            )
            if event is not None:
                events.append(event.model_dump(mode="json"))

        return events

    def create_approved_event(
        self,
        *,
        title: str,
        start_at: datetime,
        end_at: datetime,
        relevance_reason: str,
        approval_id: str | None,
        idempotency_key: str | None,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Create a calendar event only through a verified approval capability.

        This is intentionally not used by automatic outreach.  The injected
        persistent guard must verify an unexpired Human Approval for the exact
        event content and atomically reserve the idempotency key.  A plain
        caller-controlled boolean is never sufficient authority to write.
        """

        clean_approval_id = str(approval_id or "").strip()
        clean_idempotency_key = str(idempotency_key or "").strip()
        if not clean_approval_id or not clean_idempotency_key:
            return {
                "created": False,
                "status": "NOT_WRITTEN_NO_HUMAN_APPROVAL",
            }
        if self._calendar_write_guard is None:
            return {
                "created": False,
                "status": "NOT_WRITTEN_APPROVAL_GUARD_NOT_CONFIGURED",
            }

        clean_title = str(title or "").strip()
        clean_reason = str(relevance_reason or "").strip()
        if not clean_title:
            raise ValueError("Calendar event title is required.")
        if not clean_reason:
            raise ValueError("A real relevance_reason is required for Calendar writing.")

        start = self._as_riyadh_time(start_at)
        end = self._as_riyadh_time(end_at)
        if end <= start:
            raise ValueError("Calendar end_at must be later than start_at.")

        clean_description = str(description or "").strip() or None
        content_sha256 = self._calendar_write_hash(
            title=clean_title,
            start=start,
            end=end,
            relevance_reason=clean_reason,
            description=clean_description,
        )
        # Validate the integration before consuming an approval capability.
        self.settings.require_calendar_write()
        client = self._calendar_client(write=True)
        claim = self._claim_approved_write(
            approval_id=clean_approval_id,
            content_sha256=content_sha256,
            idempotency_key=clean_idempotency_key,
        )
        claim_status = str(claim.get("status") or "").upper()
        if claim_status == "ALREADY_CREATED":
            return {
                "created": False,
                "status": "ALREADY_CREATED",
                "event_id": claim.get("event_id"),
                "calendar_id": self.settings.rawaj_events_calendar_id,
            }
        if claim_status != "CLAIMED":
            return {
                "created": False,
                "status": "NOT_WRITTEN_INVALID_HUMAN_APPROVAL",
            }

        body: dict[str, Any] = {
            "summary": clean_title,
            "start": {"dateTime": start.isoformat(), "timeZone": "Asia/Riyadh"},
            "end": {"dateTime": end.isoformat(), "timeZone": "Asia/Riyadh"},
            # Do not notify external guests from the Outreach Agent.
            "guestsCanInviteOthers": False,
        }
        if clean_description:
            body["description"] = clean_description

        try:
            created = (
                client.events()
                .insert(
                    calendarId=self.settings.rawaj_events_calendar_id,
                    body=body,
                    sendUpdates="none",
                )
                .execute()
            )
        except Exception as error:  # Do not pretend an event was created.
            self._record_calendar_write_failure(
                approval_id=clean_approval_id,
                content_sha256=content_sha256,
                idempotency_key=clean_idempotency_key,
            )
            raise CalendarIntegrationError(
                "Google Calendar did not confirm event creation."
            ) from error

        event_id = str(created.get("id") or "").strip()
        if not event_id:
            self._record_calendar_write_failure(
                approval_id=clean_approval_id,
                content_sha256=content_sha256,
                idempotency_key=clean_idempotency_key,
            )
            raise CalendarIntegrationError(
                "Google Calendar returned no event ID for the created event."
            )
        self._record_calendar_write_success(
            approval_id=clean_approval_id,
            content_sha256=content_sha256,
            idempotency_key=clean_idempotency_key,
            event_id=event_id,
        )
        return {
            "created": True,
            "status": "CREATED_IN_GOOGLE_CALENDAR",
            "event_id": event_id,
            "calendar_id": self.settings.rawaj_events_calendar_id,
        }

    def _validate_read_window(self, *, start: datetime, end: datetime) -> None:
        """Bound optional reads to the near-term marketing decision window."""

        now = self._as_riyadh_time(self._now_provider())
        maximum_end = now + timedelta(days=self.settings.calendar_lookahead_days)
        minimum_end = now - timedelta(days=1)
        if end < minimum_end or start > maximum_end or end > maximum_end:
            raise ValueError(
                "Calendar reads must stay within the configured near-term "
                "calendar_lookahead_days window."
            )

    @staticmethod
    def _calendar_write_hash(
        *,
        title: str,
        start: datetime,
        end: datetime,
        relevance_reason: str,
        description: str | None,
    ) -> str:
        canonical = {
            "title": title,
            "start_at": start.isoformat(),
            "end_at": end.isoformat(),
            "relevance_reason": relevance_reason,
            "description": description,
        }
        serialized = json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def _claim_approved_write(
        self,
        *,
        approval_id: str,
        content_sha256: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Delegate atomic approval verification and idempotency to persistence."""

        try:
            claim = self._calendar_write_guard.claim_approved_calendar_write(
                approval_id=approval_id,
                content_sha256=content_sha256,
                idempotency_key=idempotency_key,
            )
        except Exception as error:
            raise CalendarIntegrationError(
                "Approved Calendar write could not be claimed safely."
            ) from error
        return claim if isinstance(claim, dict) else {"status": "REJECTED"}

    def _record_calendar_write_success(
        self,
        *,
        approval_id: str,
        content_sha256: str,
        idempotency_key: str,
        event_id: str,
    ) -> None:
        try:
            self._calendar_write_guard.record_calendar_write_success(
                approval_id=approval_id,
                content_sha256=content_sha256,
                idempotency_key=idempotency_key,
                event_id=event_id,
            )
        except Exception as error:
            # The external event might exist, so do not make an automatic retry
            # possible after a persistence failure. A human must reconcile it.
            raise CalendarIntegrationError(
                "Google Calendar created an event, but its audit record could not "
                "be completed. Manual reconciliation is required."
            ) from error

    def _record_calendar_write_failure(
        self,
        *,
        approval_id: str,
        content_sha256: str,
        idempotency_key: str,
    ) -> None:
        try:
            self._calendar_write_guard.record_calendar_write_failure(
                approval_id=approval_id,
                content_sha256=content_sha256,
                idempotency_key=idempotency_key,
            )
        except Exception:
            # Preserve the original provider error. The uncompleted claim blocks
            # an unsafe blind retry until a human resolves it.
            pass

    def _calendar_client(self, *, write: bool) -> Any:
        if self._service_factory is not None:
            return self._service_factory(write)

        scopes = [self.WRITE_SCOPE if write else self.READ_SCOPE]
        try:
            from googleapiclient.discovery import build
        except ImportError as error:
            raise CalendarIntegrationError(
                "Google Calendar libraries are not installed. Install project requirements."
            ) from error

        try:
            if self.settings.google_service_account_file:
                from google.oauth2.service_account import (
                    Credentials as ServiceAccountCredentials,
                )

                credentials = ServiceAccountCredentials.from_service_account_file(
                    self.settings.google_service_account_file,
                    scopes=scopes,
                )
            else:
                from google.auth.transport.requests import Request
                from google.oauth2.credentials import Credentials
                from google_auth_oauthlib.flow import InstalledAppFlow

                client_secret_path = self.settings.google_oauth_client_secret_path
                if not client_secret_path:
                    raise CalendarIntegrationError(
                        "No Google OAuth or service-account credential path is configured."
                    )
                credentials = self._oauth_credentials(
                    Credentials=Credentials,
                    Request=Request,
                    InstalledAppFlow=InstalledAppFlow,
                    scopes=scopes,
                )
            return build("calendar", "v3", credentials=credentials, cache_discovery=False)
        except CalendarIntegrationError:
            raise
        except Exception as error:
            raise CalendarIntegrationError(
                "Google Calendar authentication could not be completed."
            ) from error

    def _oauth_credentials(
        self,
        *,
        Credentials: Any,
        Request: Any,
        InstalledAppFlow: Any,
        scopes: list[str],
    ) -> Any:
        token_path = Path(self.settings.google_oauth_token_path)
        credentials = None
        if token_path.exists():
            credentials = Credentials.from_authorized_user_file(
                str(token_path), scopes
            )

        if credentials and credentials.valid and credentials.has_scopes(scopes):
            return credentials
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                self.settings.google_oauth_client_secret_path,
                scopes,
            )
            credentials = flow.run_local_server(port=0)

        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(credentials.to_json(), encoding="utf-8")
        if os.name != "nt":
            token_path.chmod(0o600)
        return credentials

    @staticmethod
    def _list_events(
        *,
        client: Any,
        calendar_id: str,
        start: datetime,
        end: datetime,
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        page_token: str | None = None

        try:
            while True:
                response = (
                    client.events()
                    .list(
                        calendarId=calendar_id,
                        timeMin=start.astimezone(timezone.utc).isoformat(),
                        timeMax=end.astimezone(timezone.utc).isoformat(),
                        singleEvents=True,
                        orderBy="startTime",
                        maxResults=250,
                        pageToken=page_token,
                        fields="items(id,summary,start,end,status),nextPageToken",
                    )
                    .execute()
                )
                events.extend(response.get("items", []))
                page_token = response.get("nextPageToken")
                if not page_token:
                    return events
        except Exception as error:
            raise CalendarIntegrationError(
                "Google Calendar did not return usable event data."
            ) from error

    @classmethod
    def _to_calendar_event(
        cls,
        raw_event: dict[str, Any],
        *,
        calendar_id: str,
    ) -> CalendarEvent | None:
        event_id = str(raw_event.get("id") or "").strip()
        title = str(raw_event.get("summary") or "").strip()
        start_at = cls._google_time_to_iso(raw_event.get("start") or {})
        if not event_id or not title or not start_at:
            return None

        return CalendarEvent(
            event_id=event_id,
            title=title,
            start_at=start_at,
            end_at=cls._google_time_to_iso(raw_event.get("end") or {}),
            calendar_id=calendar_id,
            # Calendar descriptions and private links may contain team-only data.
            description=None,
            source_url=None,
            relevance_reason=(
                "Verified Rawaj Calendar event in the requested contact window. "
                "Use only if it naturally fits this restaurant."
            ),
        )

    @classmethod
    def _google_time_to_iso(cls, google_time: dict[str, Any]) -> str | None:
        date_time = google_time.get("dateTime")
        if date_time:
            try:
                return cls._as_riyadh_time(
                    datetime.fromisoformat(str(date_time).replace("Z", "+00:00"))
                ).isoformat()
            except ValueError:
                return None

        all_day = google_time.get("date")
        if all_day:
            try:
                return datetime.combine(
                    date.fromisoformat(str(all_day)), time.min, tzinfo=cls.RIYADH
                ).isoformat()
            except ValueError:
                return None
        return None

    @classmethod
    def _as_riyadh_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=cls.RIYADH)
        return value.astimezone(cls.RIYADH)

    @classmethod
    def _is_marketing_event_candidate(
        cls,
        raw_event: dict[str, Any],
        restaurant_context: dict[str, Any],
    ) -> bool:
        """Keep calendar noise out without inventing restaurant-specific facts."""

        if str(raw_event.get("status") or "confirmed").casefold() == "cancelled":
            return False

        event_text = str(raw_event.get("summary") or "").casefold()
        if any(keyword in event_text for keyword in cls.OCCASION_KEYWORDS):
            return True

        # Custom events are eligible only when their *title* explicitly matches
        # a customer-safe category, cuisine, city, or team-maintained event tag.
        # Do not stringify arbitrary context: it may contain names, emails, IDs,
        # or internal notes that must not influence what Calendar data is shown.
        return any(
            term in event_text
            for term in cls._safe_context_terms(restaurant_context)
        )

    @staticmethod
    def _safe_context_terms(restaurant_context: dict[str, Any]) -> set[str]:
        allowed_fields = {
            "cuisine",
            "category",
            "restaurant_category",
            "city",
            "location",
            "event_tags",
            "calendar_tags",
        }
        terms: set[str] = set()
        for field in allowed_fields:
            value = restaurant_context.get(field)
            values = value if isinstance(value, list) else [value]
            for item in values:
                if not isinstance(item, str):
                    continue
                term = " ".join(item.casefold().split())
                if 3 <= len(term) <= 60:
                    terms.add(term)
        return terms


__all__ = ["CalendarIntegrationError", "CalendarService"]
