import hashlib
import hmac

from app.security import verify_razorpay_signature


def test_razorpay_signature_is_verified() -> None:
    body = b'{"event":"payment.failed"}'
    secret = "test-secret"
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_razorpay_signature(body, signature, secret)
    assert not verify_razorpay_signature(body, "wrong", secret)
