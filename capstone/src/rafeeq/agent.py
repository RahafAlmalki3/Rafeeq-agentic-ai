"""Rafeeq: the agent, its two eval configurations, its context rules and its token budget.

Tools: Madar's read tools over MCP (mcp_server.py, in-process, same database); write tools, email and memory locally.
Every call passes through the harness in K.run_agent: permission → the applicant's confirmation (write tools) →
guard → tool → audit. Refund requests above 1,000 SAR are then paid only after a finance officer approves them
(POL-06; api.py).
"""

from __future__ import annotations

import copy
import json
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from .guards import Audit, approve_all, approve_cli, describe, guard, redact
from .kit import agentkit as K
from .kit import madar
from .memory import Memory
from . import mcp_server

SPEC = (K.ROOT / "AGENTS.md").read_text(encoding="utf-8")
SKILLS_DIR = K.DATA / "skills"
OUT = K.lab_dir("rafeeq")  # artifacts/rafeeq: databases, audit logs, sessions, red-team runs (git-ignored)

MAX_STEPS = 8                 # model calls per request: the longest task (verify → slots → book → reply) needs 4–5
MAX_RUN_TOKENS = 40_000       # per request; each step re-sends ≈ 3k fixed tokens (spec + tools), so 8 steps ≈ 30k
MAX_SESSION_TOKENS = 150_000  # per chat session
MAX_RESULT_CHARS = 3_000      # cuts only search_policies at k=5 (up to 4.4k chars); a month of list_slots is < 1k
MAX_HISTORY_MESSAGES = 24     # compaction: last messages kept, never starting on a tool result

READ_TOOLS = set(madar.Madar.READ_TOOLS) | {"memory", "load_skill"}
WRITE_TOOLS = {"book_inspection", "reschedule_inspection", "create_refund", "escalate", "email_applicant", "send_email"}


# ---------------------------------------------------------------- skills (progressive disclosure)
def read_skill(path: Path) -> tuple[dict, str]:
    _, head, body = path.read_text(encoding="utf-8").split("---", 2)
    return dict(line.split(": ", 1) for line in head.strip().splitlines() if ": " in line), body.strip()


SKILLS = {read_skill(p)[0]["name"]: p for p in sorted(SKILLS_DIR.glob("*/SKILL.md"))}
SKILL_INDEX = "\n".join(f"- **{n}**: {read_skill(p)[0]['description']}" for n, p in SKILLS.items())


def load_skill(name: str) -> str:
    """Load the step-by-step procedure of one skill before doing that kind of multi-step task.

    Args:
        name: the skill name from the list in your instructions, e.g. "refund-request".
    """
    if name not in SKILLS:
        return json.dumps({"error": f"No skill {name!r}. Available: {sorted(SKILLS)}"})
    return read_skill(SKILLS[name])[1]


# ---------------------------------------------------------------- configurations (change ONE thing between them)
CONFIGS = {
    # A: the spec alone.
    "A": {"system": SPEC, "skills": False, "model": None},
    # B: + Agent Skills with progressive disclosure (an index in the prompt, procedures on demand). Hypothesis: the
    # multi-step action tasks (refunds, bookings, escalations) fail on skipped steps, and the procedures fix that.
    "B": {"system": SPEC + "\n## Skills\nBefore a multi-step task, call load_skill with the matching name:\n" + SKILL_INDEX,
          "skills": True, "model": None},
}


# ---------------------------------------------------------------- token budget (the cost budget is $0 on free models)
class BudgetedShape(K.ChatShape):
    """Chat Completions with a token budget per run: past it, the loop ends with a hand-over message."""

    def __init__(self, max_tokens: int):
        super().__init__()
        self.max_tokens, self.first = max_tokens, None  # not `start`: ChatShape.start() builds the first messages

    def step(self, state, tools, model, use_cache=None, **kw):
        used = K.LEDGER.input_tokens + K.LEDGER.output_tokens
        self.first = used if self.first is None else self.first
        if used - self.first > self.max_tokens:
            text = "I've reached my limit for this request. An officer will follow up with you."
            state.append({"role": "assistant", "content": text})
            return K.Turn(text, [])
        return super().step(state, tools, model, use_cache=use_cache, **kw)


# ---------------------------------------------------------------- context rules
def cap_result(result: str) -> str:
    if len(result) > MAX_RESULT_CHARS:
        return result[:MAX_RESULT_CHARS] + f"\n[… {len(result) - MAX_RESULT_CHARS} characters cut; ask for less]"
    return result


def compact(history: list) -> list:
    """Keep the last messages (never starting on a tool result), and clear tool results of earlier turns: the answers
    built from them stay, and the model can call the tool again if it needs the detail."""
    tail = history[-MAX_HISTORY_MESSAGES:]
    while tail and tail[0].get("role") != "user":
        tail = tail[1:]
    last_user = max((i for i, msg in enumerate(tail) if msg.get("role") == "user"), default=0)
    return [{**msg, "content": "[old tool result cleared; call the tool again if needed]"}
            if msg.get("role") == "tool" and i < last_user else msg for i, msg in enumerate(tail)]


# ---------------------------------------------------------------- the agent
class Rafeeq:
    """One agent bound to one Madar database, one MCP connection, one memory and one audit log.

    mode: "defended" (email_applicant + guard), "prompt-only" (raw send_email, rules in the prompt only) or
    "naive" (raw send_email, the course's original prompt, no guard): the last two exist for the red team.
    """

    def __init__(self, config: dict, m: madar.Madar, mcp: K.MCPBridge, memory: Memory, audit: Audit,
                 approve=approve_cli, mode: str = "defended"):
        self.config, self.m, self.mcp, self.memory, self.audit = config, m, mcp, memory, audit
        self.approve, self.mode = approve, mode

    def email_tool(self):
        m = self.m
        if self.mode != "defended":
            return m.send_email

        def email_applicant(permit_id: str, national_id_last4: str, mobile: str, subject: str, body: str) -> dict:
            """Email the applicant at the address registered on their application (POL-10). Verifies identity first.

            Args:
                permit_id: the applicant's permit, e.g. "REN-10482".
                national_id_last4: verification (POL-08).
                mobile: verification (POL-08).
                subject: subject line.
                body: plain-text body. Never include national IDs, mobile numbers or outside links.
            """
            p = m._permit(permit_id)
            m._verify(p, national_id_last4, mobile)
            return m.send_email(p["email"], subject, body)

        return email_applicant

    def tools(self) -> list:
        m = self.m
        local = [m.book_inspection, m.reschedule_inspection, m.create_refund, m.escalate, self.email_tool(),
                 self.memory.tool()]
        return local + ([load_skill] if self.config["skills"] else []) + self.mcp.agent_tools()

    def system(self) -> str:
        if self.mode == "naive":
            return madar.SYSTEM_PROMPT
        system = self.config["system"]
        if self.mode == "prompt-only":
            system = system.replace("email_applicant", "send_email")
        # the harness loads memory (no model call needed); it goes last so the stable prefix above stays cacheable
        return f"{system}\n\n## Remembered preferences\n{self.memory.contents().strip() or '(none yet)'}"

    def permission(self, name: str, args: dict) -> str:
        decision = "allow" if name in READ_TOOLS else "ask" if name in WRITE_TOOLS else "deny"
        self.audit.log("permission", tool=name, decision=decision)
        return decision

    def _approve(self, name: str, args: dict) -> bool | str:
        ok = self.approve(name, args)  # True / False, or a string: deferred to a Confirm button outside this run
        self.audit.log("confirmation", tool=name, action=describe(name, redact(args)),
                       outcome="deferred" if isinstance(ok, str) else "confirmed" if ok else "declined")
        return ok

    def _after(self, name: str, args: dict, result: str) -> str:
        self.audit.log("tool", tool=name, args=redact(args), chars=len(result), error=result.startswith('{"error"'))
        return cap_result(result)

    def run(self, prompt: str, history: list | None = None, verbose: bool = False,
            shape: BudgetedShape | None = None, **kw) -> K.Run:
        return K.run_agent(prompt, self.tools(), system=self.system(), model=self.config["model"], history=history,
                           shape=shape or BudgetedShape(MAX_RUN_TOKENS), max_steps=MAX_STEPS, permission=self.permission,
                           approve=self._approve, before_tool=guard if self.mode == "defended" else None,
                           after_tool=self._after, verbose=verbose, **kw)


# ---------------------------------------------------------------- preview of a write action (for the Confirm card)
def dry_run(m: madar.Madar, name: str, args: dict) -> dict:
    """Run a write tool on a throwaway copy of the database: the exact outcome (amount, slot, recipient) or the exact
    error, without changing anything and without repeating any of the tools' rules here."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        shadow = copy.copy(m)
        shadow.db_path = Path(tmp) / "madar.sqlite"
        shutil.copy(m.db_path, shadow.db_path)
        fn = (Rafeeq(CONFIGS["A"], shadow, None, None, None).email_tool() if name == "email_applicant"
              else getattr(shadow, name, None))
        if fn is None:
            return {"ok": False, "error": f"Unknown action {name!r}."}
        try:
            return {"ok": True, "result": fn(**args)}
        except madar.ToolError as e:
            return {"ok": False, "error": str(e)}
        except TypeError as e:
            return {"ok": False, "error": f"Bad arguments for {name}: {e}"}


def when(slot: str) -> str:
    d = datetime.strptime(slot, "%Y-%m-%d %H:%M")
    return f"{d:%A} {d.day} {d:%B %Y}, {d:%H:%M}"


def mask_email(address: str) -> str:
    local, _, domain = address.partition("@")
    return f"{local[:2]}{'•' * max(1, len(local) - 2)}@{domain}"


REFUND_REASONS = {"rejected": "Application rejected (fee − 100 SAR)", "withdrawn_before_review": "Withdrawn before review (full fee)",
                  "withdrawn_during_review": "Withdrawn during review (50% of the fee)", "duplicate_payment": "Duplicate payment"}
KINDS = {"health": "Health inspection", "foundation": "Foundation inspection", "site_visit": "Site visit"}


def action_details(name: str, args: dict, result: dict) -> list[list[str]]:
    """What the applicant sees on the Confirm card: [label, value] rows from the dry run's result."""
    r = result
    if name == "book_inspection":
        return [["Permit", r["permit_id"]], ["Inspection", KINDS.get(r["kind"], r["kind"])], ["District", r["district"]],
                ["When", when(r["slot"])], ["Cost", "Free · reschedule or cancel free up to 24 hours before (POL-05)"]]
    if name == "reschedule_inspection":
        return [["Inspection", r["inspection_id"]], ["From", when(r["old_slot"])], ["To", when(r["new_slot"])],
                ["Cost", "Free (more than 24 hours before the current slot, POL-05)"]]
    if name == "create_refund":
        then = ("A finance officer approves the payment first (above 1,000 SAR), then it is paid within 10 working days"
                if r["status"] == "pending_approval" else "Paid automatically to the original payment method within 10 working days")
        return [["Permit", r["permit_id"]], ["Reason", REFUND_REASONS.get(args.get("reason"), args.get("reason", ""))],
                ["Refund amount", f"{r['amount_sar']:,} SAR"], ["Then", f"{then} (POL-06)"]]
    if name == "escalate":
        return [["Permit", r["permit_id"]], ["Overdue", f"{r['working_days_overdue']} working days"],
                ["Goes to", f"The duty supervisor, who replies within {r['reply_within']} (POL-03)"],
                ["Your note", str(args.get("note", ""))]]
    if name == "email_applicant":
        return [["To", f"{mask_email(r['to'])} (the address registered on your application)"],
                ["Subject", str(args.get("subject", ""))], ["Message", str(args.get("body", ""))[:1200]]]
    return []


def eval_agent(config: dict, name: str, tokens: list, mode: str = "defended"):
    """An agent for evals.run_suite: a fresh MCP connection and memory on each task's fresh database."""

    def agent(prompt: str, m: madar.Madar, **kw) -> K.Run:
        before = K.LEDGER.input_tokens + K.LEDGER.output_tokens
        try:
            with K.MCPBridge(mcp_server.build(m)) as mcp:
                r = Rafeeq(config, m, mcp, Memory(m.workdir / "memories"), Audit(OUT / "audit_eval.jsonl", name),
                           approve=approve_all, mode=mode)
                return r.run(prompt, **kw)
        finally:
            tokens.append(K.LEDGER.input_tokens + K.LEDGER.output_tokens - before)

    agent.__name__ = name
    return agent
