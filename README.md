# Snack counter automation

A small, executable Python submission for the Framsikt exercise. It coordinates purchases, reserves office budgets, submits simulated HR claims and tracks payment separately from approval.

## Run

Requires Python 3.11 or later. No third-party packages, API keys, network access or installation needed.

```sh
python demo.py
python -m unittest -v
```

Run these commands from this directory. Tests use temporary SQLite databases and independent connections, including concurrent worker tests. The demo also uses temporary data and makes no real purchases or HR submissions.

## Files

| File | Purpose |
| --- | --- |
| `design.pdf` | Two-page design document for submission |
| `DESIGN.md` | Editable source for the design |
| `snacks.py` | Domain service, persistent storage and fake external adapters |
| `demo.py` | End-to-end scenario in both offices, including a visiting buyer |
| `test_snacks.py` | 39 automated tests of business rules and failure recovery |

## Lifecycle

```text
OPEN -> ASSIGNED -> BUYING -> REFILLED -> SUBMITTING -> SUBMITTED
                                                        |
                                                     APPROVED -> REIMBURSED
```

An HR payment notification may move directly from SUBMITTED to REIMBURSED. Rejection moves the claim to REJECTED for owner reconciliation. An unstarted refill may be CANCELLED. Decline or a four-hour assignment timeout returns a refill to OPEN and tries a different eligible volunteer. BUYING is a commitment: it cannot be reassigned automatically.

`run_worker(chat, hr)` performs one background-worker tick: assign open work, hand off expired assignments, escalate delays, submit/reconcile claims and deliver chat events. A real deployment would call it periodically and use trusted chat handlers to call the domain methods. There is no background process running after the demo exits.

## Rules worth reading

- `report_empty`: merges duplicate empty reports, reserves an estimate and prevents concurrent overspending. Request IDs identify chat events; counter IDs are stable configured identifiers.
- `_assign`: uses current office presence, opt-in availability, unpaid work and purchase history. People with any active or unreimbursed job are excluded. This limits each person's exposure, at the cost of a smaller buyer pool.
- `start_purchase`: checks the assigned identity, presence, expiry and any new-month reservation before authorizing shopping.
- `complete_refill`: the buyer confirms the counter was refilled; captures receipt reference, quantities and actual cost. Actual cost replaces the hold. A new empty cycle is allowed even while an earlier buyer awaits payment.
- `submit_claim`: persists SUBMITTING before the network call and uses the refill ID as a stable external idempotency key.
- `monthly_report`: adds actual purchase spending in each local currency, then converts the NOK aggregate to INR with an explicit Decimal rate. It excludes reservations and does not count reimbursement again.

Money is always integer minor units: 600,000 ore = 6,000 NOK; 1,500,000 paise = 15,000 INR. Both currencies have two decimal places. The demo's `1 NOK = 8 INR` is invented, not a current exchange rate. Its report is 725 NOK + 1,800 INR = 7,600 INR at that illustrative rate.

## Honest boundaries

This is core logic, not a deployed bot. Company login, authorization, webhooks, presence updates, secure receipt uploads, operational monitoring and a real scheduler remain integration work. The caller must supply an authenticated employee identity and an office-local date; users must not be allowed to impersonate another buyer or change budget dates. Registration and cancellation are administrator-only at that boundary. Purchase dates come from validated receipts, not the upload date. A receipt string stands in for a private image object; a production adapter would upload the image to HR.

The fake HR service supports duplicate-safe creation and status lookup. The supplied brief promises neither. Production must confirm these capabilities before enabling automatic retries. If a create call times out without idempotency or lookup, stop and reconcile rather than risk a second claim. Approval must never be treated as proof of payment. HR owns payroll-currency conversion; support for visiting employees needs confirmation.

Reservations prevent the system from authorizing spending above budget. They cannot prevent a person from spending extra in a shop. A real receipt above authorization is therefore recorded and flagged, even if the remaining balance becomes negative. No automatic extra budget is granted. Future reservations are blocked when funds are insufficient.

The prototype deliberately leaves rejection repair and recovery of a stalled BUYING job to the office owner. The owner must confirm whether money was spent before arranging a replacement. There is no implemented command to reset a started purchase, correct an expense or amend a rejected HR claim. These operations need an audit-preserving workflow in production. Failed HR/chat calls are returned by the worker and remain retryable; production needs backoff and alerts. Chat delivery is at least once; FakeChat deduplicates event IDs, while real chat deduplication depends on its API.

The worker retries OPEN refills when availability changes. People already offered a particular refill are excluded from that refill's later attempts; when the pool is exhausted, the owner gets NEEDS_VOLUNTEER. Availability is a supplied snapshot, not inferred from calendars. Timeouts are elapsed hours, including nights/weekends; an office-hours policy is a production improvement.

Items purchased are reported per office. They are a restocking signal, **not evidence of consumption**: stock remaining, waste and individual snack depletion are not tracked. No claim is made that this prototype knows which snacks people ate.

AI tools assisted in preparing this exercise. The design records assumptions, trade-offs and known integration gaps.

