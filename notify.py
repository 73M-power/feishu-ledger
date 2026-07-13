#!/usr/bin/env python3
"""Push ledger summaries to a Feishu custom bot webhook.

This matches the deployment style used by ai-finance-digest: GitHub Actions
wakes up, runs a Python script, posts to FEISHU_WEBHOOK, then exits.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feishu_ledger as ledger

CST = timezone(timedelta(hours=8))


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip().strip("\ufeff").strip()


def data_dir() -> Path:
    path = Path(_env("LEDGER_DATA_DIR", Path(__file__).parent / "data"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def post_feishu_webhook(webhook: str, text: str) -> dict:
    payload = {"msg_type": "text", "content": {"text": text}}
    req = urllib.request.Request(
        webhook,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read()
    data = json.loads(raw) if raw else {}
    code = data.get("code", data.get("StatusCode", 0))
    if code not in (0, None):
        raise RuntimeError(f"Feishu webhook failed: {data}")
    return data


def previous_month(now: datetime | None = None) -> str:
    now = now or datetime.now(CST)
    y, m = now.year, now.month - 1
    if m <= 0:
        y -= 1
        m = 12
    return f"{y:04d}-{m:02d}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Push shared ledger summary to Feishu webhook")
    parser.add_argument("--month", default="", help="Month in YYYY-MM, default current month")
    parser.add_argument("--previous-month", action="store_true", help="Send previous month settlement")
    parser.add_argument("--comparison-months", type=int, default=6)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    webhook = _env("FEISHU_WEBHOOK")
    if not webhook and not args.dry_run:
        raise RuntimeError("Missing FEISHU_WEBHOOK")

    data = ledger.load_ledger(data_dir())
    month = args.month or (previous_month() if args.previous_month else ledger.month_of(datetime.now(CST)))
    summary = ledger.month_summary(data, month)
    comparisons = ledger.month_comparison(data, months=args.comparison_months, now=datetime.now(CST))
    text = ledger.format_summary_reply(summary) + "\n\n" + ledger.format_comparison_reply(comparisons)

    if args.dry_run:
        print(text)
        return 0
    post_feishu_webhook(webhook, text)
    print(f"pushed ledger summary for {month}")
    return 0


if __name__ == "__main__":
    sys.exit(main())