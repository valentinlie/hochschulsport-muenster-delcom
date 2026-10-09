"""Self-check for course enrolment + auto-pay, with a fake client (no network,
no money). Run: uv run python tests/test_courses.py  (needs src/config.py)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core import courses, db  # noqa: E402
from core.delcom import DelcomError  # noqa: E402

db.record_attempt = lambda *a, **k: None
courses.RETRY_DELAY = 0

COURSE = {"id": 1, "description": "Tennis Level 2", "startDate": "2026-11-02T11:00:00.000Z",
          "price": 144, "availableParticipantCount": 3}
MANDATE = {"id": 4921, "type": 2, "preferred": True, "paymentProviderType": 1}


class Fake:
    def __init__(self, sale=None, sale_fails=0, method=MANDATE, pay=None, pay_error=None):
        self.sale = sale or {"id": 77, "statusCode": 1, "total": "99.00"}
        self.sale_fails, self.method = sale_fails, method
        self.pay_result, self.pay_error = pay, pay_error
        self.sales, self.payments = 0, []

    def create_sale(self, body):
        self.sales += 1
        if self.sales <= self.sale_fails:
            raise DelcomError("Enrolment failed (400): not open yet")
        return self.sale

    def get_course(self, cid):
        return COURSE

    def direct_debit_method(self, member_id):
        return self.method

    def pay_sale(self, sale_id, method):
        self.payments.append((sale_id, method["id"]))
        if self.pay_error:
            raise DelcomError(self.pay_error)
        return self.pay_result


def book(fake, **kw):
    return courses.book_course(1, client=fake, member={"id": 7674}, course=COURSE,
                               body={"x": 1}, retry_seconds=2, **kw)


PAID = {"userInitiatedPaymentResult": {"bSuccess": True, "bPaidOnStoredPayMethod": True},
        "sale": {"statusCode": 0}}

# grabbed on the 3rd try, then paid exactly once with the saved mandate
f = Fake(sale_fails=2, pay=PAID)
status, msg = book(f)
assert status == "success" and "paid 99.00" in msg and f.payments == [(77, 4921)], msg

# payment error: spot still counts, sale stays pending, payment never retried
f = Fake(pay_error="Payment failed (500): boom")
status, msg = book(f)
assert status == "success" and "PAY NOW" in msg and "boom" in msg and len(f.payments) == 1, msg

# a payment page instead of instant success -> pending, link kept
f = Fake(pay={"userInitiatedPaymentResult": {"bSuccess": True, "hostedPaymentPageUrl": "https://x"}})
status, msg = book(f)
assert "PAY NOW" in msg and "payment page" in msg, msg

# money guard: a total above the list price is never paid
f = Fake(sale={"id": 78, "statusCode": 1, "total": "500"}, pay=PAID)
status, msg = book(f)
assert f.payments == [] and "not auto-paid" in msg, msg

# no saved mandate / pay off / free course -> no payment
for fake, kw in [(Fake(method=None), {}), (Fake(pay=PAID), {"pay": False}),
                 (Fake(sale={"id": 79, "statusCode": 0, "total": "0"}, pay=PAID), {})]:
    status, msg = book(fake, **kw)
    assert status == "success" and fake.payments == [], msg

# dry run sends nothing at all
f = Fake(pay=PAID)
status, msg = book(f, dry_run=True)
assert status == "dry_run" and f.sales == 0 and f.payments == [] and "#4921" in msg, msg

print("ok")
