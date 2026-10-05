"""Run one safe Rawaj follow-up scheduler pass.

Use this as a cron/Task Scheduler target or call the scheduler directly from
the notebook.  Due follow-ups send automatically after content review. Only initial outreach pauses for human approval.
"""

from __future__ import annotations

import argparse
import json

try:  # Supports package imports and direct notebook imports.
    from .agent import RawajOutreachApplication
except ImportError:  # pragma: no cover - direct notebook import support.
    from agent import RawajOutreachApplication


def main() -> int:
    parser = argparse.ArgumentParser(description="Run due Rawaj follow-ups once.")
    parser.add_argument("--limit", type=int, default=100, help="Maximum relationships to inspect.")
    parser.add_argument(
        "--kind",
        choices=("prospect", "client"),
        default="prospect",
        help="Run prospect no-response reassessments or client-context reassessments.",
    )
    args = parser.parse_args()

    application = RawajOutreachApplication.create()
    result = (
        application.scheduler.run_due_followups_once(limit=args.limit)
        if args.kind == "prospect"
        else application.scheduler.run_due_client_checks_once(limit=args.limit)
    )
    print(
        json.dumps(
            {
                "scanned": result.scanned,
                "started": result.started,
                "pending_human_approval": result.paused_for_human_approval,
                "stopped_or_waiting": result.stopped_or_waiting,
                "failed": result.failed,
                "items": result.items,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
