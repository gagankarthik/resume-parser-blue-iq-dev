#!/usr/bin/env python
"""
Print a non-reversible fingerprint of a webhook registration's signing secret.

WHY THIS EXISTS
    `POST /api/v1/webhooks` returns `hmac_secret` exactly once and there is no rotate
    endpoint, so when a client reports "invalid signature" there is no safe way to ask
    "are we even talking about the same secret?" - the answer either leaks the secret
    into a chat log or stays unanswerable.

    This prints SHA-256 of the secret, truncated. Both sides run their own copy against
    the value they hold and compare the fingerprint, never the secret:

        MATCH     -> the secret is correct. The mismatch is in the receiver's
                     verification. In practice that is almost always a body-parser
                     consuming the raw bytes, so the receiver signs a RE-SERIALIZED
                     body: `HMAC(secret, timestamp + "." + json.dumps(parsed))` does
                     not equal `HMAC(secret, timestamp + "." + raw_bytes)`.
                     See docs/CLIENT_INTEGRATION_GUIDE.md §6.
        DIFFERENT -> the two sides hold different secrets. Either the receiver was
                     configured from another registration, or the registration was
                     recreated after the secret was handed over.

    A fingerprint is not a credential: it cannot be used to sign anything, and the
    secret is not recoverable from it. It is safe to paste into a ticket.

USAGE
    # ours, read from DynamoDB (needs read access to the webhooks table)
    python scripts/webhook_secret_fingerprint.py --company-id gighealth-1f4cd3

    # one registration only
    python scripts/webhook_secret_fingerprint.py \
        --company-id gighealth-1f4cd3 --webhook-id 01KYMMD337H5V02NB08F0HJPFA

    # theirs, from the value they have configured (no AWS access needed).
    # Prefer the env form - a secret passed as an argv value lands in shell history
    # and in the process list.
    BLUEIQ_WEBHOOK_SECRET=... python scripts/webhook_secret_fingerprint.py --stdin-secret

The equivalent one-liner, for a client who would rather not run our script at all:

    node  -e 'console.log(require("crypto").createHash("sha256").update(process.env.S).digest("hex").slice(0,12))'
    python -c 'import hashlib,os;print(hashlib.sha256(os.environ["S"].encode()).hexdigest()[:12])'
"""

import argparse
import hashlib
import os
import sys

FINGERPRINT_CHARS = 12


def fingerprint(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()[:FINGERPRINT_CHARS]


def _describe(secret: str) -> str:
    """Length is a useful second signal: a truncated paste is a common failure, and
    ours are always token_hex(32) -> 64 hex characters."""
    shape = "64 hex chars (expected)" if len(secret) == 64 else f"{len(secret)} chars (UNEXPECTED)"
    return f"sha256={fingerprint(secret)}  len={shape}"


def _from_dynamodb(company_id: str, webhook_id: str | None) -> int:
    try:
        from app.db.webhooks import get_webhook, list_webhooks
    except ImportError as exc:  # pragma: no cover - operator convenience
        print(f"Run from the repo root with the project venv active: {exc}", file=sys.stderr)
        return 2

    if webhook_id:
        hook = get_webhook(company_id, webhook_id)
        hooks = [hook] if hook else []
    else:
        hooks = list_webhooks(company_id)

    if not hooks:
        print(f"No webhook registrations found for company_id={company_id}", file=sys.stderr)
        return 1

    print(f"{len(hooks)} registration(s) for {company_id}:\n")
    for hook in hooks:
        secret = hook.get("hmac_secret") or ""
        print(f"  webhook_id : {hook.get('webhook_id')}")
        print(f"  url        : {hook.get('url')}")
        print(f"  status     : {hook.get('status')}")
        print(f"  events     : {', '.join(hook.get('events', []))}")
        print(f"  created_at : {hook.get('created_at')}")
        print(f"  secret     : {_describe(secret) if secret else 'MISSING'}")
        print()

    if len(hooks) > 1:
        print("More than one ACTIVE registration: every event is delivered to all of "
              "them, each signed with its OWN secret. A receiver configured from the "
              "wrong one rejects 100% of its deliveries.\n")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--company-id", help="fingerprint the stored secret(s) for this company")
    ap.add_argument("--webhook-id", help="restrict to one registration")
    ap.add_argument("--stdin-secret", action="store_true",
                    help="fingerprint the secret in $BLUEIQ_WEBHOOK_SECRET, or on stdin")
    args = ap.parse_args()

    if args.stdin_secret:
        secret = os.environ.get("BLUEIQ_WEBHOOK_SECRET") or sys.stdin.read().strip()
        if not secret:
            print("No secret supplied (set BLUEIQ_WEBHOOK_SECRET or pipe it in).",
                  file=sys.stderr)
            return 1
        print(_describe(secret))
        return 0

    if not args.company_id:
        ap.error("pass --company-id, or --stdin-secret")
    return _from_dynamodb(args.company_id, args.webhook_id)


if __name__ == "__main__":
    raise SystemExit(main())
