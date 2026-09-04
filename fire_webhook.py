import argparse
import hashlib
import hmac
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.config import PROJECT_ROOT, get_settings


DEFAULT_URL = "https://vanish-copied-uptown.ngrok-free.dev/webhooks/razorpay"


def main() -> int:
    parser = argparse.ArgumentParser(description="Send a signed Razorpay webhook body.")
    parser.add_argument(
        "body_file",
        nargs="?",
        type=Path,
        default=PROJECT_ROOT / "razorpay_cards.md",
        help="Path to the raw JSON body file.",
    )
    parser.add_argument("--url", default=DEFAULT_URL, help="Webhook URL.")
    args = parser.parse_args()

    body_path = args.body_file if args.body_file.is_absolute() else Path.cwd() / args.body_file
    try:
        body = body_path.read_bytes()
    except OSError as exc:
        print(f"Could not read body file: {exc}", file=sys.stderr)
        return 1

    secret = get_settings().razorpay_webhook_secret
    if not secret or secret == "replace-me":
        print("RAZORPAY_WEBHOOK_SECRET is not configured.", file=sys.stderr)
        return 1

    signature = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    request = Request(
        args.url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": signature,
        },
    )

    try:
        with urlopen(request, timeout=30) as response:
            response_body = response.read().decode("utf-8", errors="replace")
            print(f"HTTP {response.status}")
            print(response_body)
    except HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        print(f"HTTP {exc.code}", file=sys.stderr)
        print(error_body, file=sys.stderr)
        return 1
    except URLError as exc:
        print(f"Request failed: {exc.reason}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())