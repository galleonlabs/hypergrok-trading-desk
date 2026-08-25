from __future__ import annotations

from pathlib import Path

from trading_harness.learning_bridge import LearningRecorder
from trading_harness.learning_ledger import DecisionClass, LearningLedger
from trading_harness.post_trade_review import PostTradeReviewer
from trading_harness.staging_inbox import TrustedQuoteRequest
from trading_harness.tool_api import ToolService
from tests.test_learning_quote_service import LearningQuoteServiceTests
from tests.test_node import AT


class LearningBridgeTests(LearningQuoteServiceTests):
    def test_saved_analysis_and_staged_ticket_become_distinct_learning_cycles(self) -> None:
        ledger = LearningLedger(
            Path(self.temporary.name) / "learning-events.sqlite3",
            clock=lambda: AT,
        )
        recorder = LearningRecorder(ledger)
        analysis_record = self.research.get_asset_analysis(
            self.analysis["analysis_hash"]
        )
        analysis_cycle, analysis_event = recorder.record_analysis(analysis_record)
        decision = self.service()(
            TrustedQuoteRequest("eth", self.analysis["analysis_hash"])
        )
        assert decision.ticket_payload is not None
        trade_cycle, trade_event = recorder.record_staged_ticket(
            decision.ticket_payload
        )

        self.assertIs(analysis_cycle.classification, DecisionClass.BUY)
        self.assertIn("advisory_analysis", analysis_cycle.tags)
        self.assertIn("unit_quantity_research_bracket", analysis_cycle.tags)
        self.assertIs(trade_cycle.classification, DecisionClass.BUY)
        self.assertIn("infrastructure_learning", trade_cycle.tags)
        self.assertNotEqual(analysis_cycle.cycle_id, trade_cycle.cycle_id)
        self.assertNotEqual(analysis_event.event_hash, trade_event.event_hash)
        reviews = PostTradeReviewer(ledger).review_all()
        self.assertEqual(2, len(reviews))
        self.assertTrue(all(not review.close_outcome_recorded for review in reviews))
        self.assertTrue(ledger.verify_integrity())
        tools = ToolService(
            market_brief_reader=lambda *_args, **_kwargs: {},
            learning_ledger=ledger,
        )
        review = tools.get_learning_review(trade_cycle.cycle_id)
        summary = tools.get_learning_summary()
        self.assertEqual(trade_cycle.cycle_id, review["cycle_id"])
        self.assertEqual(2, summary["group_count"])
        self.assertIn("no_causality", summary["interpretation_boundary"])


if __name__ == "__main__":
    import unittest

    unittest.main()
