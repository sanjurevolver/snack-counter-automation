"""Run with: python demo.py. All data and external services are simulated."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from snacks import FakeChat, FakeHR, SnackService


def main():
    now = [datetime(2026, 11, 16, 9, tzinfo=timezone.utc)]
    today = date(2026, 11, 16)
    with TemporaryDirectory() as directory:
        service = SnackService(Path(directory) / "demo.sqlite", lambda: now[0])
        chat, hr = FakeChat(), FakeHR()
        service.register("oslo-1", "NOK", "Oslo")
        service.register("oslo-2", "NOK", "Oslo")
        service.register("visitor", "NOK", "Bengaluru")
        oslo = service.report_empty("chat-oslo-1", "Oslo", "main", 80_000, today)
        print("Oslo assigned:", service.assign(oslo))
        now[0] += timedelta(hours=5)
        service.sweep()
        print("Expired assignment handed to:", service.get(oslo)["buyer"])
        service.start_purchase(oslo, "oslo-2", today)
        service.complete_refill(oslo, "oslo-2", 72_500, "NOK", "simulated-receipt-oslo.jpg", {"nuts": 5}, today)
        bengaluru = service.report_empty("chat-blr-1", "Bengaluru", "main", 200_000, today)
        buyer = service.assign(bengaluru)
        service.start_purchase(bengaluru, buyer, today)
        service.complete_refill(bengaluru, buyer, 180_000, "INR", "simulated-receipt-blr.jpg", {"fruit": 12}, today)
        hr.timeout_after_create = True
        try:
            service.submit_claim(oslo, hr)
        except TimeoutError:
            print("HR response lost; retry uses the same claim key.")
        for refill in (oslo, bengaluru):
            service.submit_claim(refill, hr)
            hr.claims[refill]["status"] = "APPROVED"
            service.sync_claim(refill, hr)
            print("Approval is not payment:", service.get(refill)["state"])
            hr.claims[refill]["status"] = "REIMBURSED"
            service.sync_claim(refill, hr)
        service.flush_chat(chat)
        print("HR claims:", len(hr.claims), "Chat events:", len(chat.messages))
        print("Report (illustrative rate, not a market quote):")
        print(service.monthly_report("2026-11", Decimal("8.00"), "Demo rate: 2026-11, 1 NOK = 8 INR"))
        print("Bengaluru budget:", service.budget("Bengaluru", "2026-11"))


if __name__ == "__main__":
    main()
