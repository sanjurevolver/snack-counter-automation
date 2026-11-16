"""Snack refill domain service. Python 3.11+, standard library only."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import json
import sqlite3
import uuid


class RuleError(ValueError):
    pass


class FakeChat:
    """An unavailable chat never loses an event. Delivery keys deduplicate retries."""
    def __init__(self):
        self.messages = {}
        self.available = True

    def send(self, key, payload):
        if not self.available:
            raise ConnectionError("Chat unavailable")
        self.messages.setdefault(key, payload)


class FakeHR:
    """Simulates an idempotent create API plus an optional status API.

    Neither idempotency nor status lookup is promised by the exercise's HR API.
    A real integration must negotiate them or reconcile uncertain sends manually.
    """
    def __init__(self):
        self.claims = {}
        self.available = True
        self.timeout_after_create = False

    def create_claim(self, key, payload):
        if not self.available:
            raise ConnectionError("HR unavailable")
        claim = self.claims.setdefault(key, {
            "id": "HR-" + key, "payload": payload, "status": "SUBMITTED"
        })
        if self.timeout_after_create:
            self.timeout_after_create = False
            raise TimeoutError("HR accepted claim but response was lost")
        return claim["id"]

    def status(self, key):
        if not self.available:
            raise ConnectionError("HR unavailable")
        return self.claims[key]["status"]


SCHEMA = """
CREATE TABLE IF NOT EXISTS offices (
    id TEXT PRIMARY KEY, currency TEXT NOT NULL, monthly_limit INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS employees (
    id TEXT PRIMARY KEY, payroll_currency TEXT NOT NULL,
    present_office TEXT REFERENCES offices(id), available INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS refills (
    id TEXT PRIMARY KEY, office TEXT NOT NULL REFERENCES offices(id),
    counter TEXT NOT NULL, state TEXT NOT NULL,
    month TEXT NOT NULL, reserved INTEGER NOT NULL, actual INTEGER,
    buyer TEXT REFERENCES employees(id), deadline TEXT,
    receipt TEXT, items TEXT, hr_id TEXT, over_budget INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_refill
    ON refills(office,counter) WHERE state IN ('OPEN','ASSIGNED','BUYING');
CREATE TABLE IF NOT EXISTS reports (
    request_id TEXT PRIMARY KEY, refill_id TEXT NOT NULL REFERENCES refills(id)
);
CREATE TABLE IF NOT EXISTS attempts (
    refill_id TEXT NOT NULL, employee_id TEXT NOT NULL,
    PRIMARY KEY(refill_id,employee_id)
);
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY, refill_id TEXT NOT NULL, kind TEXT NOT NULL,
    payload TEXT NOT NULL, created TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0
);
"""


class SnackService:
    def __init__(self, database, clock=None):
        self.database = str(database)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        with self._tx() as db:
            db.executescript(SCHEMA)
            db.executemany("INSERT OR IGNORE INTO offices VALUES (?,?,?)", [
                ("Oslo", "NOK", 600_000), ("Bengaluru", "INR", 1_500_000)
            ])

    @contextmanager
    def _tx(self):
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _money(value):
        if type(value) is not int or value <= 0:
            raise RuleError("Money must be a positive integer in minor units")

    @staticmethod
    def _month(office_date):
        # The trusted adapter supplies the office-local business date.
        from datetime import date
        if type(office_date) is not date:
            raise RuleError("Expected an office-local date")
        return office_date.strftime("%Y-%m")

    def _event(self, db, refill_id, kind, **data):
        key = str(uuid.uuid4())
        payload = dict(refill_id=refill_id, kind=kind, **data)
        db.execute("INSERT INTO events VALUES (?,?,?,?,?,0)",
                   (key, refill_id, kind, json.dumps(payload), self.clock().isoformat()))

    @staticmethod
    def _get(db, refill_id):
        row = db.execute("SELECT * FROM refills WHERE id=?", (refill_id,)).fetchone()
        if row is None:
            raise RuleError("Unknown refill")
        return row

    @staticmethod
    def _totals(db, office, month):
        row = db.execute("""SELECT COALESCE(SUM(actual),0) spent,
            COALESCE(SUM(CASE WHEN actual IS NULL AND state != 'CANCELLED'
            THEN reserved ELSE 0 END),0) held FROM refills WHERE office=? AND month=?""",
            (office, month)).fetchone()
        limit = db.execute("SELECT monthly_limit FROM offices WHERE id=?", (office,)).fetchone()
        if limit is None:
            raise RuleError("Unknown office")
        return dict(spent=row["spent"], reserved=row["held"], limit=limit[0],
                    available=limit[0] - row["spent"] - row["held"])

    def register(self, employee_id, payroll_currency, present_office, available=True):
        if not employee_id or payroll_currency not in ("NOK", "INR"):
            raise RuleError("Employee ID and supported payroll currency required")
        with self._tx() as db:
            db.execute("""INSERT INTO employees VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                payroll_currency=excluded.payroll_currency, present_office=excluded.present_office,
                available=excluded.available""",
                (employee_id, payroll_currency, present_office, int(available)))

    def report_empty(self, request_id, office, counter, estimate, office_date):
        self._money(estimate)
        month = self._month(office_date)
        if not request_id or not counter.strip():
            raise RuleError("Request ID and counter required")
        with self._tx() as db:
            prior = db.execute("SELECT refill_id FROM reports WHERE request_id=?", (request_id,)).fetchone()
            if prior:
                return prior[0]
            active = db.execute("""SELECT id FROM refills WHERE office=? AND counter=?
                AND state IN ('OPEN','ASSIGNED','BUYING')""", (office, counter)).fetchone()
            if active:
                refill_id = active[0]
            else:
                if self._totals(db, office, month)["available"] < estimate:
                    raise RuleError("Monthly budget exhausted; reduce estimate or ask owner")
                refill_id = str(uuid.uuid4())
                db.execute("""INSERT INTO refills(id,office,counter,state,month,reserved)
                    VALUES (?,?,?,'OPEN',?,?)""", (refill_id, office, counter, month, estimate))
                self._event(db, refill_id, "EMPTY", office=office, counter=counter)
            db.execute("INSERT INTO reports VALUES (?,?)", (request_id, refill_id))
            return refill_id

    def _assign(self, db, refill_id):
        r = self._get(db, refill_id)
        if r["state"] != "OPEN":
            raise RuleError("Only open refills can be assigned")
        # Rotate by completed purchases; never give a second unpaid job to a buyer.
        # Skip everyone already attempted for this refill to avoid endless ping-pong.
        person = db.execute("""SELECT e.id FROM employees e
            WHERE e.available=1 AND e.present_office=?
            AND NOT EXISTS (SELECT 1 FROM refills r WHERE r.buyer=e.id
                AND r.state NOT IN ('CANCELLED','REIMBURSED'))
            AND NOT EXISTS (SELECT 1 FROM attempts a WHERE a.refill_id=? AND a.employee_id=e.id)
            ORDER BY (SELECT COUNT(*) FROM refills r WHERE r.buyer=e.id AND r.actual IS NOT NULL),
                e.id LIMIT 1""",
            (r["office"], refill_id)).fetchone()
        if person is None:
            if not db.execute("SELECT 1 FROM events WHERE refill_id=? AND kind='NEEDS_VOLUNTEER'",
                              (refill_id,)).fetchone():
                self._event(db, refill_id, "NEEDS_VOLUNTEER", office=r["office"])
            return None
        buyer = person[0]
        deadline = (self.clock() + timedelta(hours=4)).isoformat()
        db.execute("UPDATE refills SET state='ASSIGNED',buyer=?,deadline=? WHERE id=?",
                   (buyer, deadline, refill_id))
        db.execute("INSERT INTO attempts VALUES (?,?)", (refill_id, buyer))
        currency = db.execute("SELECT currency FROM offices WHERE id=?", (r["office"],)).fetchone()[0]
        self._event(db, refill_id, "ASSIGNED", buyer=buyer, deadline=deadline,
                    office=r["office"], counter=r["counter"],
                    authorized_minor=r["reserved"], currency=currency)
        return buyer

    def assign(self, refill_id):
        with self._tx() as db:
            return self._assign(db, refill_id)

    def handoff(self, refill_id, declining_buyer=None):
        with self._tx() as db:
            r = self._get(db, refill_id)
            if r["state"] != "ASSIGNED":
                raise RuleError("Only an unstarted assignment can be handed off")
            if declining_buyer is not None:
                if declining_buyer != r["buyer"]:
                    raise RuleError("Only assigned buyer can decline")
            elif self.clock().isoformat() < r["deadline"]:
                raise RuleError("Assignment has not expired")
            self._event(db, refill_id, "HANDOFF", previous_buyer=r["buyer"])
            db.execute("UPDATE refills SET state='OPEN',buyer=NULL,deadline=NULL WHERE id=?", (refill_id,))
            return self._assign(db, refill_id)

    def _move_hold(self, db, r, month):
        if month == r["month"] and self._totals(db, r["office"], month)["available"] < 0:
            raise RuleError("An overspend requires owner review before further shopping")
        if month != r["month"]:
            if self._totals(db, r["office"], month)["available"] < r["reserved"]:
                raise RuleError("New month's budget cannot cover this purchase")
            db.execute("UPDATE refills SET month=? WHERE id=?", (month, r["id"]))

    def start_purchase(self, refill_id, buyer, office_date):
        month = self._month(office_date)
        with self._tx() as db:
            r = self._get(db, refill_id)
            if r["state"] == "BUYING" and r["buyer"] == buyer:
                return  # Duplicate button click.
            if r["state"] != "ASSIGNED" or r["buyer"] != buyer:
                raise RuleError("Only the current assigned buyer can start")
            if self.clock().isoformat() >= r["deadline"]:
                raise RuleError("Assignment expired")
            e = db.execute("SELECT * FROM employees WHERE id=?", (buyer,)).fetchone()
            if not e["available"] or e["present_office"] != r["office"]:
                raise RuleError("Buyer is not available at this office")
            self._move_hold(db, r, month)
            db.execute("UPDATE refills SET state='BUYING',deadline=? WHERE id=?",
                       ((self.clock() + timedelta(hours=24)).isoformat(), refill_id))
            self._event(db, refill_id, "BUYING", buyer=buyer, authorized_minor=r["reserved"])

    def complete_refill(self, refill_id, buyer, actual, currency, receipt, items, office_date):
        self._money(actual)
        month = self._month(office_date)
        if not receipt or not receipt.strip() or not items or any(
                not isinstance(k, str) or not k.strip() or type(v) is not int or v <= 0
                for k, v in items.items()):
            raise RuleError("Receipt reference and positive item quantities required")
        encoded_items = json.dumps(items, sort_keys=True)
        with self._tx() as db:
            r = self._get(db, refill_id)
            expected = db.execute("SELECT currency FROM offices WHERE id=?", (r["office"],)).fetchone()[0]
            if r["buyer"] != buyer or currency != expected:
                raise RuleError("Wrong buyer or purchase currency")
            if r["actual"] is not None:
                if (r["actual"], r["receipt"], r["items"], r["month"]) == (actual, receipt, encoded_items, month):
                    return
                raise RuleError("A recorded purchase cannot be overwritten")
            if r["state"] != "BUYING":
                raise RuleError("Purchase must be started first")
            # Record money already spent even if a buyer exceeded authorization.
            # Hiding/rejecting a real receipt would corrupt accounting and hurt the buyer.
            available = self._totals(db, r["office"], month)["available"]
            if month == r["month"]:
                available += r["reserved"]
            exception = actual > r["reserved"] or actual > available
            db.execute("""UPDATE refills SET state='REFILLED',actual=?,reserved=0,month=?,
                receipt=?,items=?,over_budget=? WHERE id=?""",
                (actual, month, receipt, encoded_items, int(exception), refill_id))
            self._event(db, refill_id, "REFILLED", actual_minor=actual, currency=currency)
            if exception:
                self._event(db, refill_id, "BUDGET_EXCEPTION", actual_minor=actual)

    def cancel_unstarted(self, refill_id):
        """Owner-only command at the adapter boundary; releases an unused hold."""
        with self._tx() as db:
            r = self._get(db, refill_id)
            if r["state"] not in ("OPEN", "ASSIGNED"):
                raise RuleError("Started purchases need manual reconciliation")
            db.execute("UPDATE refills SET state='CANCELLED',reserved=0 WHERE id=?", (refill_id,))
            self._event(db, refill_id, "CANCELLED")

    def submit_claim(self, refill_id, hr):
        with self._tx() as db:
            r = self._get(db, refill_id)
            if r["hr_id"]:
                return r["hr_id"]
            if r["state"] not in ("REFILLED", "SUBMITTING"):
                raise RuleError("A receipt is required before claiming")
            currency = db.execute("SELECT currency FROM offices WHERE id=?", (r["office"],)).fetchone()[0]
            payload = dict(employee_id=r["buyer"], amount_minor=r["actual"], currency=currency,
                           description=f"Snacks: {r['office']} / {r['counter']} / {refill_id}",
                           receipt_image=r["receipt"])
            db.execute("UPDATE refills SET state='SUBMITTING' WHERE id=?", (refill_id,))
        claim_id = hr.create_claim(refill_id, payload)  # Stable key across retries and process restarts.
        with self._tx() as db:
            r = self._get(db, refill_id)
            if not r["hr_id"]:
                db.execute("UPDATE refills SET state='SUBMITTED',hr_id=?,deadline=? WHERE id=?",
                           (claim_id, (self.clock() + timedelta(days=10)).isoformat(), refill_id))
                self._event(db, refill_id, "CLAIM_SUBMITTED", claim_id=claim_id)
        return claim_id

    def sync_claim(self, refill_id, hr):
        status = hr.status(refill_id)
        if status not in ("SUBMITTED", "APPROVED", "REIMBURSED", "REJECTED"):
            raise RuleError("Unknown HR status")
        ranks = {"SUBMITTED": 0, "APPROVED": 1, "REIMBURSED": 2}
        with self._tx() as db:
            r = self._get(db, refill_id)
            if not r["hr_id"]:
                raise RuleError("Claim has not been linked to HR")
            if r["state"] == "REIMBURSED" or r["state"] == status:
                return
            if status == "REJECTED":
                db.execute("UPDATE refills SET state='REJECTED' WHERE id=?", (refill_id,))
            elif r["state"] == "REJECTED":
                return  # Explicit owner reconciliation required; do not create another claim.
            elif ranks[status] < ranks[r["state"]]:
                return  # Ignore stale external events.
            else:
                db.execute("UPDATE refills SET state=? WHERE id=?", (status, refill_id))
            self._event(db, refill_id, status)

    def sweep(self):
        """Scheduler entry point: hand off expired offers, flag stalled work."""
        with self._tx() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM refills WHERE state='OPEN' OR deadline <= ?",
                                               (self.clock().isoformat(),))]
        for r in rows:
            if r["state"] == "OPEN":
                try:
                    self.assign(r["id"])
                except RuleError:
                    pass
            elif r["state"] == "ASSIGNED":
                try:
                    self.handoff(r["id"])
                except RuleError:
                    pass  # Another worker already acted.
            elif r["state"] in ("BUYING", "SUBMITTED", "APPROVED"):
                with self._tx() as db:
                    current = self._get(db, r["id"])
                    kind = "STALLED_" + current["state"]
                    if current["state"] not in ("BUYING", "SUBMITTED", "APPROVED"):
                        continue
                    if not db.execute("SELECT 1 FROM events WHERE refill_id=? AND kind=?",
                                      (r["id"], kind)).fetchone():
                        self._event(db, r["id"], kind, buyer=current["buyer"])

    def run_worker(self, chat, hr):
        """One scheduler tick; failures stay durable and do not block other claims.

        Retry is safe with FakeHR's contract. Do not wire an unknown real HR API
        into this worker until idempotency/reconciliation support is confirmed.
        """
        self.sweep()
        errors = []
        with self._tx() as db:
            rows = [dict(r) for r in db.execute("""SELECT id,state FROM refills WHERE state IN
                ('REFILLED','SUBMITTING','SUBMITTED','APPROVED')""")]
        for r in rows:
            try:
                if r["state"] in ("REFILLED", "SUBMITTING"):
                    self.submit_claim(r["id"], hr)
                else:
                    self.sync_claim(r["id"], hr)
            except (ConnectionError, TimeoutError) as exc:
                errors.append((r["id"], str(exc)))
        try:
            self.flush_chat(chat)
        except (ConnectionError, TimeoutError) as exc:
            errors.append(("chat", str(exc)))
        return errors

    def flush_chat(self, chat):
        with self._tx() as db:
            pending = [dict(r) for r in db.execute("SELECT * FROM events WHERE delivered=0 ORDER BY rowid")]
        for event in pending:
            chat.send(event["id"], json.loads(event["payload"]))
            with self._tx() as db:
                db.execute("UPDATE events SET delivered=1 WHERE id=?", (event["id"],))

    def get(self, refill_id):
        with self._tx() as db:
            return dict(self._get(db, refill_id))

    def budget(self, office, month):
        with self._tx() as db:
            return self._totals(db, office, month)

    def monthly_report(self, month, nok_to_inr, rate_reference):
        """Reporting conversion only: original-currency ledgers remain authoritative."""
        if not isinstance(nok_to_inr, Decimal) or not nok_to_inr.is_finite() or nok_to_inr <= 0 or not rate_reference:
            raise RuleError("Positive Decimal reporting rate and provenance required")
        with self._tx() as db:
            nok = self._totals(db, "Oslo", month)["spent"]
            inr = self._totals(db, "Bengaluru", month)["spent"]
            converted = int((Decimal(nok) * nok_to_inr).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            items = {}
            for r in db.execute("SELECT office,items FROM refills WHERE month=? AND actual IS NOT NULL", (month,)):
                for name, count in json.loads(r["items"]).items():
                    key = r["office"] + ": " + name
                    items[key] = items.get(key, 0) + count
            return dict(month=month, NOK_minor=nok, INR_minor=inr, total_INR_minor=inr + converted,
                        NOK_to_INR=str(nok_to_inr), rate_reference=rate_reference,
                        items_purchased=items)
