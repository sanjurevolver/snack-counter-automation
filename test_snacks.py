from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest

from snacks import FakeChat, FakeHR, RuleError, SnackService


class SnackTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "test.sqlite"
        self.now = datetime(2026, 11, 16, 9, tzinfo=timezone.utc)
        self.day = date(2026, 11, 16)
        self.s = SnackService(self.path, lambda: self.now)
        self.hr, self.chat = FakeHR(), FakeChat()
        self.s.register("alice", "NOK", "Oslo")
        self.s.register("bob", "NOK", "Oslo")
        self.s.register("chitra", "INR", "Bengaluru")
        self.sequence = 0

    def new(self, office="Oslo", counter="main", estimate=100_000):
        self.sequence += 1
        return self.s.report_empty(str(self.sequence), office, counter, estimate, self.day)

    def purchase(self, office="Oslo", counter="main", amount=80_000, estimate=100_000):
        refill = self.new(office, counter, estimate)
        buyer = self.s.assign(refill)
        self.s.start_purchase(refill, buyer, self.day)
        currency = "NOK" if office == "Oslo" else "INR"
        self.s.complete_refill(refill, buyer, amount, currency, "receipt.jpg", {"nuts": 2}, self.day)
        return refill

    def reimburse(self, refill):
        self.s.submit_claim(refill, self.hr)
        self.hr.claims[refill]["status"] = "REIMBURSED"
        self.s.sync_claim(refill, self.hr)

    def race(self, function):
        barrier = Barrier(2)
        def run(number):
            barrier.wait()
            try:
                return function(number)
            except RuleError:
                return "rejected"
        with ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(run, (0, 1)))

    def test_simultaneous_empty_reports_share_one_refill_and_hold(self):
        results = self.race(lambda n: self.s.report_empty(f"race-{n}", "Oslo", "main", 100_000, self.day))
        self.assertEqual(results[0], results[1])
        self.assertEqual(self.s.budget("Oslo", "2026-11")["reserved"], 100_000)

    def test_two_workers_cannot_assign_two_buyers(self):
        refill = self.new()
        result = self.race(lambda n: self.s.assign(refill))
        self.assertEqual(result.count("rejected"), 1)
        self.assertIn(self.s.get(refill)["buyer"], ("alice", "bob"))

    def test_two_people_cannot_commit_to_same_refill(self):
        refill = self.new()
        self.s.assign(refill)
        result = self.race(lambda n: self.s.start_purchase(refill, ("alice", "bob")[n], self.day))
        self.assertEqual(result.count("rejected"), 1)
        self.assertEqual(self.s.get(refill)["buyer"], "alice")

    def test_concurrent_reservations_cannot_oversubscribe_budget(self):
        result = self.race(lambda n: self.s.report_empty(f"budget-{n}", "Oslo", str(n), 400_000, self.day))
        self.assertEqual(result.count("rejected"), 1)
        self.assertEqual(self.s.budget("Oslo", "2026-11")["available"], 200_000)

    def test_expired_assignment_handed_to_someone_else(self):
        refill = self.new()
        self.s.assign(refill)
        self.now += timedelta(hours=4)
        self.s.sweep()
        self.assertEqual(self.s.get(refill)["buyer"], "bob")
        with self.assertRaises(RuleError):
            self.s.start_purchase(refill, "alice", self.day)

    def test_handoff_before_expiry_is_rejected(self):
        refill = self.new()
        self.s.assign(refill)
        with self.assertRaises(RuleError):
            self.s.handoff(refill)

    def test_decline_hands_off_immediately(self):
        refill = self.new()
        self.s.assign(refill)
        self.assertEqual(self.s.handoff(refill, "alice"), "bob")

    def test_wrong_buyer_cannot_decline(self):
        refill = self.new()
        self.s.assign(refill)
        with self.assertRaises(RuleError):
            self.s.handoff(refill, "bob")

    def test_started_purchase_is_escalated_not_reassigned(self):
        refill = self.new()
        self.s.assign(refill)
        self.s.start_purchase(refill, "alice", self.day)
        self.now += timedelta(days=2)
        self.s.sweep()
        self.s.sweep()
        self.s.flush_chat(self.chat)
        self.assertEqual(self.s.get(refill)["buyer"], "alice")
        self.assertEqual(self.s.get(refill)["state"], "BUYING")
        self.assertEqual(sum(m["kind"] == "STALLED_BUYING" for m in self.chat.messages.values()), 1)
        with self.assertRaises(RuleError):
            self.s.handoff(refill)

    def test_start_at_expiry_is_rejected(self):
        refill = self.new()
        self.s.assign(refill)
        self.now += timedelta(hours=4)
        with self.assertRaises(RuleError):
            self.s.start_purchase(refill, "alice", self.day)

    def test_hybrid_and_holiday_staff_are_not_selected(self):
        self.s.register("alice", "NOK", None)
        self.s.register("bob", "NOK", "Oslo", False)
        refill = self.new()
        self.assertIsNone(self.s.assign(refill))
        self.s.run_worker(self.chat, self.hr)
        self.assertEqual(sum(m["kind"] == "NEEDS_VOLUNTEER" for m in self.chat.messages.values()), 1)

    def test_returning_volunteer_is_picked_up_by_scheduler(self):
        self.s.register("alice", "NOK", None)
        self.s.register("bob", "NOK", None)
        refill = self.new()
        self.s.sweep()
        self.s.register("bob", "NOK", "Oslo")
        self.s.sweep()
        self.assertEqual(self.s.get(refill)["buyer"], "bob")

    def test_presence_checked_again_before_purchase(self):
        refill = self.new()
        self.s.assign(refill)
        self.s.register("alice", "NOK", "Bengaluru")
        with self.assertRaises(RuleError):
            self.s.start_purchase(refill, "alice", self.day)

    def test_visitor_spends_in_office_currency_claim_keeps_employee_id(self):
        self.s.register("alice", "NOK", "Bengaluru")
        refill = self.purchase("Bengaluru", amount=150_000, estimate=200_000)
        self.s.submit_claim(refill, self.hr)
        claim = self.hr.claims[refill]["payload"]
        self.assertEqual((claim["employee_id"], claim["currency"], claim["amount_minor"]),
                         ("alice", "INR", 150_000))

    def test_unpaid_volunteer_gets_no_second_assignment(self):
        first = self.purchase()
        self.assertEqual(self.s.get(first)["buyer"], "alice")
        self.assertEqual(self.s.assign(self.new(counter="other")), "bob")

    def test_fairness_prefers_fewer_completed_purchases(self):
        self.reimburse(self.purchase())
        self.assertEqual(self.s.assign(self.new()), "bob")

    def test_actual_cost_replaces_hold_and_releases_difference(self):
        self.purchase()
        self.assertEqual(self.s.budget("Oslo", "2026-11"),
                         dict(spent=80_000, reserved=0, limit=600_000, available=520_000))

    def test_exact_budget_limit_is_allowed(self):
        self.new(estimate=600_000)
        with self.assertRaises(RuleError):
            self.new(counter="other", estimate=1)

    def test_cancellation_releases_hold(self):
        refill = self.new(estimate=600_000)
        self.s.cancel_unstarted(refill)
        self.assertEqual(self.s.budget("Oslo", "2026-11")["available"], 600_000)

    def test_started_purchase_cannot_be_cancelled(self):
        refill = self.new()
        self.s.assign(refill)
        self.s.start_purchase(refill, "alice", self.day)
        with self.assertRaises(RuleError):
            self.s.cancel_unstarted(refill)

    def test_unexpected_overspend_is_recorded_flagged_and_blocks_new_spend(self):
        refill = self.purchase(amount=650_000)
        self.assertEqual(self.s.get(refill)["over_budget"], 1)
        self.assertEqual(self.s.budget("Oslo", "2026-11")["available"], -50_000)
        with self.assertRaises(RuleError):
            self.new(estimate=1)

    def test_reservation_moves_to_new_month_before_shopping(self):
        refill = self.new()
        self.s.assign(refill)
        self.s.start_purchase(refill, "alice", date(2026, 12, 1))
        self.assertEqual(self.s.budget("Oslo", "2026-11")["reserved"], 0)
        self.assertEqual(self.s.budget("Oslo", "2026-12")["reserved"], 100_000)

    def test_overspend_freezes_an_existing_assignment_before_it_starts(self):
        first = self.new()
        self.s.assign(first)
        self.s.start_purchase(first, "alice", self.day)
        second = self.new(counter="other")
        self.s.assign(second)
        self.s.complete_refill(first, "alice", 550_000, "NOK", "r.jpg", {"nuts": 1}, self.day)
        with self.assertRaises(RuleError):
            self.s.start_purchase(second, "bob", self.day)
        self.assertEqual(self.s.get(second)["state"], "ASSIGNED")

    def test_month_rollover_budget_failure_leaves_original_hold_intact(self):
        refill = self.new()
        self.s.assign(refill)
        self.s.report_empty("dec", "Oslo", "other", 600_000, date(2026, 12, 1))
        with self.assertRaises(RuleError):
            self.s.start_purchase(refill, "alice", date(2026, 12, 1))
        self.assertEqual(self.s.get(refill)["state"], "ASSIGNED")
        self.assertEqual(self.s.budget("Oslo", "2026-11")["reserved"], 100_000)

    def test_purchase_date_sets_accounting_month_even_if_purchase_crosses_month(self):
        refill = self.new()
        self.s.assign(refill)
        self.s.start_purchase(refill, "alice", self.day)
        self.s.complete_refill(refill, "alice", 50_000, "NOK", "r.jpg", {"nuts": 1}, date(2026, 12, 1))
        self.assertEqual(self.s.budget("Oslo", "2026-11")["reserved"], 0)
        self.assertEqual(self.s.budget("Oslo", "2026-12")["spent"], 50_000)

    def test_duplicate_receipt_event_does_not_double_spend(self):
        refill = self.purchase()
        self.s.complete_refill(refill, "alice", 80_000, "NOK", "receipt.jpg", {"nuts": 2}, self.day)
        self.assertEqual(self.s.budget("Oslo", "2026-11")["spent"], 80_000)
        with self.assertRaises(RuleError):
            self.s.complete_refill(refill, "alice", 90_000, "NOK", "receipt.jpg", {"nuts": 2}, self.day)

    def test_old_empty_event_does_not_create_refill_after_completion(self):
        refill = self.purchase()
        self.assertEqual(self.s.report_empty("1", "Oslo", "main", 100_000, self.day), refill)
        self.assertNotEqual(self.new(), refill)

    def test_invalid_money_is_rejected(self):
        for value in (0, -1, 1.5, True):
            with self.subTest(value=value), self.assertRaises(RuleError):
                self.new(estimate=value)

    def test_wrong_currency_missing_receipt_and_wrong_actor_rejected(self):
        refill = self.new()
        self.s.assign(refill)
        self.s.start_purchase(refill, "alice", self.day)
        for buyer, currency, receipt in (("bob", "NOK", "r.jpg"), ("alice", "INR", "r.jpg"), ("alice", "NOK", "")):
            with self.subTest(buyer=buyer, currency=currency, receipt=receipt), self.assertRaises(RuleError):
                self.s.complete_refill(refill, buyer, 80_000, currency, receipt, {"nuts": 1}, self.day)
        self.assertEqual(self.s.get(refill)["state"], "BUYING")

    def test_claim_cannot_precede_purchase(self):
        with self.assertRaises(RuleError):
            self.s.submit_claim(self.new(), self.hr)

    def test_lost_hr_response_is_retried_without_duplicate_claim(self):
        refill = self.purchase()
        self.hr.timeout_after_create = True
        with self.assertRaises(TimeoutError):
            self.s.submit_claim(refill, self.hr)
        self.assertEqual(self.s.get(refill)["state"], "SUBMITTING")
        restarted = SnackService(self.path, lambda: self.now)
        restarted.submit_claim(refill, self.hr)
        restarted.submit_claim(refill, self.hr)
        self.assertEqual(len(self.hr.claims), 1)
        self.assertEqual(restarted.get(refill)["state"], "SUBMITTED")

    def test_approval_is_not_payment_and_stale_status_does_not_regress(self):
        refill = self.purchase()
        self.s.submit_claim(refill, self.hr)
        self.hr.claims[refill]["status"] = "APPROVED"
        self.s.sync_claim(refill, self.hr)
        self.assertEqual(self.s.get(refill)["state"], "APPROVED")
        self.hr.claims[refill]["status"] = "SUBMITTED"
        self.s.sync_claim(refill, self.hr)
        self.assertEqual(self.s.get(refill)["state"], "APPROVED")
        self.reimburse(refill)
        self.assertEqual(self.s.get(refill)["state"], "REIMBURSED")

    def test_rejected_claim_escalates_without_new_claim_or_releasing_spend(self):
        refill = self.purchase()
        self.s.submit_claim(refill, self.hr)
        self.hr.claims[refill]["status"] = "REJECTED"
        self.s.run_worker(self.chat, self.hr)
        self.assertEqual(self.s.get(refill)["state"], "REJECTED")
        self.assertEqual(self.s.budget("Oslo", "2026-11")["spent"], 80_000)
        self.s.run_worker(self.chat, self.hr)
        self.assertEqual(len(self.hr.claims), 1)

    def test_late_hr_claim_gets_escalation(self):
        refill = self.purchase()
        self.s.submit_claim(refill, self.hr)
        self.now += timedelta(days=11)
        self.s.run_worker(self.chat, self.hr)
        self.assertTrue(any(m["kind"] == "STALLED_SUBMITTED" for m in self.chat.messages.values()))

    def test_worker_recovers_from_hr_and_chat_outage(self):
        refill = self.purchase()
        self.hr.available = self.chat.available = False
        self.assertEqual(len(self.s.run_worker(self.chat, self.hr)), 2)
        self.hr.available = self.chat.available = True
        self.assertEqual(self.s.run_worker(self.chat, self.hr), [])
        self.assertEqual(self.s.get(refill)["state"], "SUBMITTED")
        delivered = len(self.chat.messages)
        self.s.flush_chat(self.chat)
        self.assertEqual(len(self.chat.messages), delivered)

    def test_report_converts_only_after_separate_currency_totals(self):
        self.purchase(amount=72_500)
        self.purchase("Bengaluru", amount=180_000, estimate=200_000)
        report = self.s.monthly_report("2026-11", Decimal("8.00"), "Illustrative monthly rate")
        self.assertEqual((report["NOK_minor"], report["INR_minor"], report["total_INR_minor"]),
                         (72_500, 180_000, 760_000))
        self.assertEqual(report["items_purchased"], {"Oslo: nuts": 2, "Bengaluru: nuts": 2})
        self.assertEqual(self.s.budget("Oslo", "2026-11")["spent"], 72_500)

    def test_conversion_uses_decimal_half_up_rounding(self):
        self.purchase(amount=1, estimate=1)
        self.assertEqual(self.s.monthly_report("2026-11", Decimal("8.5"), "test")["total_INR_minor"], 9)

    def test_report_excludes_reservations_and_other_months(self):
        self.new()
        self.purchase("Bengaluru")
        report = self.s.monthly_report("2026-12", Decimal("8"), "test")
        self.assertEqual(report["total_INR_minor"], 0)
        self.assertEqual(self.s.monthly_report("2026-11", Decimal("8"), "test")["NOK_minor"], 0)

    def test_reporting_rate_must_be_positive_decimal_with_provenance(self):
        for rate, source in ((8.0, "test"), (Decimal("NaN"), "test"), (Decimal("-1"), "test"), (Decimal("8"), "")):
            with self.subTest(rate=rate), self.assertRaises(RuleError):
                self.s.monthly_report("2026-11", rate, source)


if __name__ == "__main__":
    unittest.main()
