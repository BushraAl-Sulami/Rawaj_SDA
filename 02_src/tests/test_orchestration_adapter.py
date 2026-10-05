"""Offline checks for the small outer-pipeline Outreach bridge."""

from __future__ import annotations

import unittest

from orchestration.outreach_node import outreach_node


class OutreachPipelineAdapterTests(unittest.TestCase):
    def test_forwards_exact_upstream_ids_to_outreach_runtime(self) -> None:
        received: dict[str, int] = {}

        def fake_runner(**kwargs: int) -> dict[str, object]:
            received.update(kwargs)
            return {
                "outreach_thread_id": "thread_101",
                "outreach_status": "PENDING_OUTBOUND_APPROVAL",
                "outreach_action": "SEND_INITIAL_OUTREACH",
                "outreach_message_id": "outbound_101",
                "outreach_pending_human_approval": True,
                "outreach_errors": [],
            }

        result = outreach_node(
            {
                "restaurant_id": 101,
                "research_run_id": 1001,
                "qualification_run_id": 2001,
            },
            outreach_runner=fake_runner,
        )

        self.assertEqual(
            received,
            {
                "restaurant_id": 101,
                "research_run_id": 1001,
                "qualification_run_id": 2001,
            },
        )
        self.assertEqual(result["outreach_thread_id"], "thread_101")
        self.assertTrue(result["outreach_pending_human_approval"])
        self.assertEqual(result["next"], "end")

    def test_missing_id_returns_truthful_pipeline_error(self) -> None:
        result = outreach_node(
            {
                "restaurant_id": 101,
                "research_run_id": 1001,
            }
        )

        self.assertIn("qualification_run_id", result["error"])
        self.assertEqual(result["next"], "end")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
