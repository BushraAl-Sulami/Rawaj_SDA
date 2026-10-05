"""Explicit handoff boundary between Outreach and the teammate Strategy Agent.

Outreach never generates a strategy.  After a verified Interested event, it
creates an immutable request and places it in a real local outbox that the
team's Strategy Agent can consume.  The adapter is replaceable when the team
publishes its direct Strategy Agent interface.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

try:  # Supports package imports and direct local notebook imports.
    from .config import RawajSettings
    from .schemas import StrategyOutputHandoff, StrategyRequestHandoff
except ImportError:  # pragma: no cover - used by a direct notebook import.
    from config import RawajSettings
    from schemas import StrategyOutputHandoff, StrategyRequestHandoff


class StrategyHandoffError(RuntimeError):
    """Raised when a Strategy request or output cannot be safely exchanged."""


class FileStrategyHandoffDispatcher:
    """A durable local contract until the teammate Strategy Agent is wired in.

    It writes a request only.  It never creates strategy content, completion,
    a PDF, dashboard URL, or a success status on behalf of the Strategy Agent.
    """

    def __init__(self, settings: RawajSettings) -> None:
        self.settings = settings
        self.outbox_dir = Path(settings.strategy_handoff_outbox)

    def dispatch_strategy_request(self, request: dict[str, Any]) -> dict[str, Any]:
        """Persist one validated request, idempotently, for the Strategy Agent."""

        handoff = StrategyRequestHandoff.model_validate(request)
        payload = handoff.model_dump(mode="json")
        payload_hash = self._payload_hash(payload)
        path = self.outbox_dir / f"{handoff.strategy_request_id}.json"

        self.outbox_dir.mkdir(parents=True, exist_ok=True)
        if path.exists():
            existing = self._read_json(path)
            if self._payload_hash(existing) != payload_hash:
                raise StrategyHandoffError(
                    "A different Strategy request already uses this request ID."
                )
            return {
                "status": "ALREADY_DISPATCHED",
                "strategy_request_id": handoff.strategy_request_id,
                "path": str(path),
            }

        self._atomic_write(path, payload)
        return {
            "status": "DISPATCHED_TO_STRATEGY_OUTBOX",
            "strategy_request_id": handoff.strategy_request_id,
            "path": str(path),
        }

    @staticmethod
    def load_strategy_output(payload: dict[str, Any]) -> StrategyOutputHandoff:
        """Validate an output actually supplied by the teammate Strategy Agent."""

        return StrategyOutputHandoff.model_validate(payload)

    @staticmethod
    def _payload_hash(payload: dict[str, Any]) -> str:
        # ``created_at`` may be defaulted independently on a safe graph retry;
        # it is audit metadata, not part of the immutable Strategy request.
        canonical_payload = dict(payload)
        canonical_payload.pop("created_at", None)
        serialized = json.dumps(
            canonical_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise StrategyHandoffError(
                "Existing Strategy handoff cannot be read safely."
            ) from error
        if not isinstance(result, dict):
            raise StrategyHandoffError("Existing Strategy handoff is not an object.")
        return result

    @staticmethod
    def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
        temporary_path = path.with_suffix(f".{os.getpid()}.tmp")
        try:
            temporary_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary_path, path)
        except OSError as error:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise StrategyHandoffError(
                "Strategy request could not be written to the outbox."
            ) from error


__all__ = ["FileStrategyHandoffDispatcher", "StrategyHandoffError"]
