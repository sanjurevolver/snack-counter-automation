# Snack counter automation

## 1. Scope and assumptions

Use a chat workflow backed by one small service, SQLite and a periodic worker. Start with explicit "empty" reports and human confirmation of refill; no sensors or separate app. Each office has a named owner for exceptions. Volunteers opt in and keep current office/availability up to date, including visits, home working and leave. Company login supplies identity in a production adapter. Budgets are hard limits on authorized purchases: Oslo 6,000 NOK/month and Bengaluru 15,000 INR/month.

Before production, confirm HR's idempotency, status/paid notification and foreign-currency claim support; management's reporting currency and FX policy; and the owner, presence source and reminder hours. For this prototype, HR converts the original expense to payroll currency, reporting uses INR with an explicit illustrative rate, and deadlines use elapsed hours. These are assumptions, not capabilities promised in the brief.

## 2. From empty to reimbursed

1. **OPEN:** A volunteer reports an empty counter with a purchase estimate. Atomically merge reports for the same active counter and reserve the estimate against the office's monthly budget. An insufficient budget returns a clear rejection for the owner to resolve; it never creates an unfunded assignment.
2. **ASSIGNED:** Choose an available volunteer present at that office, excluding anyone with active or unpaid work and anyone already tried for this refill. Prefer the fewest completed purchases; break ties deterministically by employee ID. Send the buyer, counter, authorized amount and four-hour deadline through chat.
3. **BUYING:** Only that buyer may start, before expiry; recheck presence and budget month. Declines or expired unstarted assignments go to the next candidate. If nobody qualifies, notify the owner and retry open work as availability changes. After shopping starts, never auto-reassign: flag a 24-hour delay for reconciliation to avoid a second purchase.
4. **REFILLED:** Buyer confirms refill and supplies the receipt image reference, purchase date, actual cost and item quantities. Replace the reservation with actual spending; release any saving. Record and flag an unexpected overspend rather than hiding a real liability. The counter may start a new refill cycle while the previous claim is pending.
5. **SUBMITTING -> SUBMITTED -> APPROVED -> REIMBURSED:** The worker sends employee ID, amount, original currency, description and receipt to simulated HR. Persist SUBMITTING before sending; use one stable claim key on retries. Approval is not payment. Only explicit paid status marks reimbursement complete. Escalate rejection or a claim pending for ten days; preserve spend and reconcile the same claim instead of creating another.

## 3. Parts and stored data

**Chat adapter:** authenticated employee and event IDs, stable office/counter IDs, commands and reminders. FakeChat stores delivered messages by event ID. **Domain service + SQLite:** offices/currencies/limits; volunteer IDs, payroll currency, current office and availability; refill state, buyer, deadline, reservation, actual cost, accounting month, receipt reference and quantities; attempted buyers, deduplicated request IDs, HR claim IDs and timestamped events. **Worker:** assignment expiry, delay escalation, claim submission/status reconciliation and an event outbox for chat. **HR adapter:** the claim payload and external claim ID/status; FakeHR simulates failures, duplicate-safe creation and payment. HR remains the approval/payment authority. Private receipt storage and real authentication are production integrations, not implemented here.

## 4. Correctness, budgets and reporting

SQLite write transactions serialize competing assignments and budget checks. A partial unique index permits only one OPEN/ASSIGNED/BUYING refill per counter. State and buyer checks reject a late buyer after handoff. Events are written in the same transaction as state changes; chat failure leaves them pending. Stable request IDs stop old chat retries from opening a new cycle. This prevents duplicate authorized purchases, provided buyers use the workflow.

Store money as integer minor units and never add NOK directly to INR. Available budget = office limit - actual purchases - outstanding reservations. At purchase start, moving into another month requires a new budget check. A recorded receipt belongs to its office-local purchase month, even if entered later; real costs that exceed authorization are retained and escalated. Reimbursement timing never changes purchase spending. A visiting employee's purchase charges the visited office in its currency; HR handles payroll conversion.

One monthly report includes both local-currency totals and a consolidated INR total. Supply a finance-approved NOK-to-INR rate and its dated reference; use Decimal and round the converted NOK aggregate once to paise, half up. The demo rate is illustrative. Preserve local amounts for audit; do not reuse the reporting rate for payroll. Purchased quantities provide a restocking signal, not proven consumption. Measuring preferences accurately would require per-item remaining/waste counts, deliberately deferred.

## 5. Trade-offs, boundaries and evidence

One database and worker are sufficient for about 35 volunteers; SQLite transactions make the concurrency rules concrete with no infrastructure setup. A production server could use PostgreSQL with equivalent constraints. Excluding all unpaid buyers limits personal financial exposure but may leave too few volunteers; the owner handles that shortage. Rotation by purchase count is simple and explainable, though it does not equalize the value or effort of purchases. An expired assignment may hand off automatically; an in-progress purchase requires human confirmation of what happened.

The external API contract is the main uncertainty. FakeHR provides idempotency and status lookup solely to demonstrate recovery. Without real support, an ambiguous send must stop for manual reconciliation; do not blindly retry. Without payment evidence, keep the claim pending. Authenticated handlers, receipt access controls, verified office dates, persistent external adapters, retry backoff, monitoring, and audited correction/rejection workflows are required before deployment.

Deliberately omit a new HR/payment system, automatic purchasing, receipt OCR, inventory sensors, individual consumption tracking, a mobile app and microservices. The submission implements the lifecycle, selection, budget reservations, report and simulated integrations. Its 39 automated tests exercise concurrent commits and reservations, expiry/handoff, stale buyers, hybrid/visiting staff, unpaid-buyer exclusion, month boundaries, exact currency totals, duplicate messages, HR timeout recovery, rejection, and approval versus payment.


