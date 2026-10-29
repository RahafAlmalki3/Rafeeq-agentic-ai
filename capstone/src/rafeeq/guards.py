"""Controls enforced in code, whatever the model was told: the data-policy guard, the redacted audit log, approvals."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
ID_RE = re.compile(r"\b[12]\d{9}\b")
MOBILE_RE = re.compile(r"(?:\+966|00966|0)?5\d{8}\b")
LINK_RE = re.compile(r"https?://([^/\s)\"'>]+)")
PERMIT_RE = re.compile(r"\b(?:BLD|REN|SGN|FTR|EVT)-\d{5}\b", re.I)
DIGITS_RE = re.compile(r"\d{4,}")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


# ---------------------------------------------------------------- the guard (before_tool)
def guard(name: str, args: dict) -> str | None:
    """before_tool: block what must never happen. Returns an error string to block the call, None to let it run."""
    reasons = []
    if name in ("email_applicant", "send_email"):
        text = f"{args.get('subject', '')}\n{args.get('body', '')}".translate(ARABIC_DIGITS)
        if ID_RE.search(text) or MOBILE_RE.search(text):
            reasons.append("the email contains a national ID or mobile number")
        if any(not host.lower().endswith("madar.example") for host in LINK_RE.findall(text)):
            reasons.append("links may only point to madar.example")
    if name == "memory" and args.get("command") in ("create", "str_replace", "insert"):
        text = " ".join(str(args.get(k) or "") for k in ("file_text", "new_str", "insert_text")).translate(ARABIC_DIGITS)
        if DIGITS_RE.search(text) or PERMIT_RE.search(text) or EMAIL_RE.search(text):
            reasons.append("memory may hold preferences only: no ID digits, mobiles, permit IDs or emails (PDPL)")
    return json.dumps({"error": "Blocked by Madar's data policy: " + "; ".join(reasons)}) if reasons else None


# ---------------------------------------------------------------- audit log (PDPL: redacted)
def redact(args: dict) -> dict:
    out = {}
    for k, v in args.items():
        if k == "national_id_last4":
            out[k] = "****"
        elif k == "mobile":
            out[k] = "…" + re.sub(r"\D", "", str(v))[-2:]
        elif k in ("body", "file_text", "new_str", "insert_text"):
            out[k] = f"<{len(str(v))} chars>"
        else:
            out[k] = v
    return out


def mask_text(text: str) -> str:
    """Free text for display to staff: full national IDs and mobile numbers keep only their last 2 digits."""
    def mask(match: re.Match) -> str:
        return "•" * (len(match.group()) - 2) + match.group()[-2:]
    return MOBILE_RE.sub(mask, ID_RE.sub(mask, text.translate(ARABIC_DIGITS)))


class Audit:
    def __init__(self, path: Path, session: str):
        self.path, self.session = path, session

    def log(self, event: str, **data) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "session": self.session, "event": event,
                                **data}, ensure_ascii=False, default=str) + "\n")


# ---------------------------------------------------------------- confirmations
def describe(name: str, args: dict) -> str:
    """The action in plain words, for the person who confirms it."""
    a = args
    return {
        "book_inspection": f"Book an inspection for {a.get('permit_id')} at {a.get('slot')}",
        "reschedule_inspection": f"Move inspection {a.get('inspection_id')} to {a.get('new_slot')}",
        "create_refund": f"Create a refund request for {a.get('permit_id')} (reason: {a.get('reason')})",
        "escalate": f"Escalate {a.get('permit_id')} to the duty supervisor: {a.get('note')}",
        "email_applicant": f"Email the registered applicant of {a.get('permit_id')}: \"{a.get('subject')}\"",
        "send_email": f"Email {a.get('to')}: \"{a.get('subject')}\"",
    }.get(name, f"{name}({json.dumps(redact(a), ensure_ascii=False)})")


def approve_cli(name: str, args: dict) -> bool:
    """The applicant confirms in the terminal (POL-10: "confirm the slot with the applicant")."""
    print(f"\n🛡️  Please confirm: {describe(name, args)}")
    if name in ("email_applicant", "send_email"):
        print("   body:", str(args.get("body", ""))[:400].replace("\n", "\n         "))
    return input("   Confirm? [y/N] ").strip().lower() == "y"


def approve_all(name: str, args: dict) -> bool:  # unattended runs: task prompts give consent; db graders check writes
    return True
