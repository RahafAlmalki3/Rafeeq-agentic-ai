"""Rafeeq as an HTTP service: the applicant confirms every write action, and a finance officer approves the payment
of refunds above 1,000 SAR (POL-10, POL-06).

    uv run rafeeq serve --port 8793

Web app (one plain-HTML page, web/app.html, that calls the endpoints below):
    GET  /                                     #applicant: permit types → chat; #officer: the finance console
    GET  /officer                              redirects to /#officer
Applicants (header X-API-Key, keys from RAFEEQ_API_KEYS):
    POST /v1/chat                              {"session_id", "message"} → reply, tools, actions to confirm
    GET  /v1/chat/{session_id}                 the conversation so far and the status of its actions
    POST /v1/chat/{session_id}/confirmations/{id}   {"confirm": true | false} → runs (or cancels) the action, and the
                                               agent continues with the result
Finance officers (header X-Officer-Key, keys from RAFEEQ_OFFICER_KEYS):
    GET  /v1/officer/refunds                   refund requests above 1,000 SAR waiting for approval (POL-06)
    POST /v1/officer/refunds/{refund_id}/approve | reject
    GET  /v1/officer/decided                   the latest finance decisions
    GET  /v1/officer/escalations               SLA escalations sent to the duty supervisor (read-only)
GET /healthz

How a write action works (the loop in agentkit.run_agent, with a deferred approval):
1. The agent calls a write tool. The data-policy guard runs first; a blocked action never reaches the applicant.
2. A dry run on a throwaway copy of the database gives the exact outcome (refund amount, slot, recipient) for the
   Confirm card, or the exact error, which goes straight back to the model (no doomed Confirm buttons).
3. The call is not executed: it becomes a confirmation with that preview, and the model is told
   "awaiting_confirmation", so it asks the applicant to press Confirm.
4. Confirm → the guard runs again, then the tool, with exactly the arguments the applicant saw; the result goes back to
   the agent as the next turn, and the agent continues (e.g. "Booked: INS-9002, Thursday 08:00"). Cancel → nothing
   changes, and the agent is told so.

Also: sessions in SQLite (compacted like `rafeeq chat`), a token budget per session (HTTP 429), HTTP 503 when the model
is unavailable, and OpenTelemetry spans (invoke_agent → chat / execute_tool) in artifacts/rafeeq/api/traces.jsonl.
Every start is a fresh pilot: a new copy of the synthetic Madar data, and no sessions or open confirmations.
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from pydantic import BaseModel, Field

from . import mcp_server
from .agent import (CONFIGS, MAX_RUN_TOKENS, MAX_SESSION_TOKENS, BudgetedShape, Rafeeq, action_details, compact,
                    dry_run)
from .guards import Audit, describe, guard, mask_text, redact
from .kit import agentkit as K
from .kit import madar
from .memory import Memory

API_KEYS = {k.strip() for k in os.getenv("RAFEEQ_API_KEYS", "dev-key-1").split(",") if k.strip()}
OFFICER_KEYS = {k.strip() for k in os.getenv("RAFEEQ_OFFICER_KEYS", "officer-key-1").split(",") if k.strip()}
SESSION_TOKENS = int(os.getenv("RAFEEQ_SESSION_TOKENS", str(MAX_SESSION_TOKENS)))
WEB = Path(__file__).resolve().parent / "web"
EVENT = "[app event] "  # marks harness-written turns (a confirm or cancel); the chat shows them as events


# ---------------------------------------------------------------- traces
class JsonlExporter(SpanExporter):
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def export(self, spans) -> SpanExportResult:
        with self.path.open("a", encoding="utf-8") as f:
            for s in spans:
                f.write(json.dumps({"name": s.name, "trace_id": f"{s.context.trace_id:032x}",
                                    "span_id": f"{s.context.span_id:016x}",
                                    "parent_id": f"{s.parent.span_id:016x}" if s.parent else None,
                                    "ms": round((s.end_time - s.start_time) / 1e6, 1),
                                    "attributes": dict(s.attributes or {})}, ensure_ascii=False) + "\n")
        return SpanExportResult.SUCCESS


class TracedShape(BudgetedShape):
    """The budgeted Chat Completions shape with one `chat {model}` span per model call."""

    def __init__(self, tracer, max_tokens: int):
        super().__init__(max_tokens)
        self.tracer = tracer

    def step(self, state, tools, model, use_cache=None, **kw):
        with self.tracer.start_as_current_span(f"chat {model}") as span:
            span.set_attribute("gen_ai.operation.name", "chat")
            span.set_attribute("gen_ai.request.model", model)
            turn = super().step(state, tools, model, use_cache=use_cache, **kw)
            u = turn.usage.model_dump() if hasattr(turn.usage, "model_dump") else (turn.usage or {})
            span.set_attribute("gen_ai.usage.input_tokens", u.get("prompt_tokens") or 0)
            span.set_attribute("gen_ai.usage.output_tokens", u.get("completion_tokens") or 0)
            span.set_attribute("gen_ai.response.finish_reasons", ["tool_calls" if turn.calls else "stop"])
            return turn


def traced(tracer, fn):
    @functools.wraps(fn)  # keeps the signature and __tool_schema__, so the tool definition is unchanged
    def wrapper(*a, **kw):
        with tracer.start_as_current_span(f"execute_tool {fn.__name__}") as span:
            span.set_attribute("gen_ai.operation.name", "execute_tool")
            span.set_attribute("gen_ai.tool.name", fn.__name__)
            return fn(*a, **kw)
    return wrapper


class ServiceRafeeq(Rafeeq):
    """Rafeeq whose tools emit spans."""

    def __init__(self, *a, tracer, **kw):
        super().__init__(*a, **kw)
        self.tracer = tracer

    def tools(self) -> list:
        return [traced(self.tracer, t) for t in super().tools()]


# ---------------------------------------------------------------- the service
class ChatIn(BaseModel):
    session_id: str = Field(min_length=1, max_length=64, pattern=r"^[\w.-]+$")
    message: str = Field(min_length=1, max_length=4000)


class ConfirmIn(BaseModel):
    confirm: bool


class TurnOut(BaseModel):
    session_id: str
    reply: str
    tools: list[str]
    confirmations: list[dict]  # actions proposed in this turn, waiting for the applicant's Confirm or Cancel
    tokens: dict
    model_steps: int


class ConfirmOut(TurnOut):
    action: dict  # the action that was just confirmed or cancelled, with its new status and result


def create_app(workdir: Path | None = None, config: str = "A") -> FastAPI:
    workdir = workdir or K.lab_dir("rafeeq/api")
    m = madar.Madar(workdir, actor="rafeeq-api")
    db = workdir / "service.sqlite"

    @contextlib.contextmanager
    def connect():
        """Commit and close (sqlite3's own `with` only commits, which leaks connections and locks the file)."""
        con = sqlite3.connect(db)
        try:
            with con:
                yield con
        finally:
            con.close()

    db.unlink(missing_ok=True)  # a fresh pilot: sessions and confirmations belong to the database copied above
    with connect() as con:
        con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, history TEXT, tokens INTEGER, updated REAL)")
        con.execute("CREATE TABLE confirmations (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, tool TEXT, "
                    "args TEXT, action TEXT, details TEXT, status TEXT, created REAL, decided REAL, result TEXT)")
        con.execute("CREATE TABLE decisions (refund_id TEXT, permit_id TEXT, amount_sar INTEGER, status TEXT, "
                    "officer TEXT, ts REAL)")
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(JsonlExporter(workdir / "traces.jsonl")))
    tracer = provider.get_tracer("rafeeq")
    lock = threading.Lock()  # one agent run at a time: K.LEDGER is process-wide (fine for a pilot, not for scale)
    audit = Audit(workdir / "audit.jsonl", "service")
    state: dict = {}

    @contextlib.asynccontextmanager
    async def lifespan(app):
        state["mcp"] = K.MCPBridge(mcp_server.build(m)).open()  # one MCP connection, the same database as the writes
        executor = Rafeeq(CONFIGS[config], m, state["mcp"], Memory(workdir / "memories" / "_executor"), audit)
        state["tools"] = {t.__name__: t for t in executor.tools()}  # runs confirmed actions, with these exact tools
        yield
        state["mcp"].close()

    app = FastAPI(title="Rafeeq API", version="1.1.0", lifespan=lifespan)

    # ------------------------------------------------------------ helpers
    def client_key(x_api_key: str = Header(default="")) -> str:
        if x_api_key not in API_KEYS:
            raise HTTPException(401, "Missing or invalid X-API-Key")
        return x_api_key

    def officer_key(x_officer_key: str = Header(default="")) -> str:
        if x_officer_key not in OFFICER_KEYS:
            raise HTTPException(401, "Missing or invalid X-Officer-Key")
        return x_officer_key

    def sql(query: str, params: tuple = ()) -> list[dict]:
        with connect() as con:
            con.row_factory = sqlite3.Row
            return [dict(r) for r in con.execute(query, params)]

    def session_key(key: str, session_id: str) -> str:
        return f"{hashlib.sha256(key.encode()).hexdigest()[:12]}:{session_id}"  # sessions are private to a key

    def short_hash(secret: str) -> str:
        return hashlib.sha256(secret.encode()).hexdigest()[:12]

    def loads(text: str | None):
        return json.loads(text) if (text or "").startswith(("{", "[")) else text

    def public(row: dict) -> dict:
        """An action as the applicant sees it, including the refund's finance status (POL-06)."""
        status, result = row["status"], loads(row["result"])
        if row["tool"] == "create_refund" and status == "done" and isinstance(result, dict):
            refund = m.sql("SELECT amount_sar, status FROM refunds WHERE refund_id = ?", (result.get("refund_id"),))
            if refund and refund[0]["amount_sar"] > 1000:
                status = {"pending_approval": "finance_pending", "approved": "finance_approved",
                          "rejected": "finance_rejected"}.get(refund[0]["status"], status)
        return {"id": row["id"], "action": row["action"], "details": json.loads(row["details"] or "[]"),
                "status": status, "result": result}

    def run_turn(sid: str, message: str) -> tuple[K.Run, dict, list[dict]]:
        """One agent turn in a session: budget check, the run, new confirmations, and the history saved."""
        rows = sql("SELECT history, tokens FROM sessions WHERE id = ?", (sid,))
        history, spent = (json.loads(rows[0]["history"]), rows[0]["tokens"]) if rows else ([], 0)
        if spent >= SESSION_TOKENS:
            raise HTTPException(429, f"Session budget of {SESSION_TOKENS} tokens used up. Start a new session.")
        proposed: list[dict] = []

        def ask_applicant(name: str, args: dict) -> str:
            """The deferred approval: never now; the applicant presses Confirm or Cancel in the app."""
            blocked = guard(name, args)  # an action the policy forbids is never offered for confirmation
            if blocked:
                return blocked
            preview = dry_run(m, name, args)  # the exact outcome, on a throwaway copy of the database
            if not preview["ok"]:  # it would fail (slot taken, not eligible, wrong ID): tell the model now
                return json.dumps({"error": preview["error"]}, ensure_ascii=False)
            action, details = describe(name, redact(args)), action_details(name, args, preview["result"])
            proposed.append({"tool": name, "args": args, "action": action, "details": details})
            return json.dumps({"status": "awaiting_confirmation", "action": action, "preview": dict(details),
                               "note": "Nothing has happened yet. The applicant sees this preview with Confirm and "
                                       "Cancel buttons. Tell them what you propose and ask them to press Confirm. "
                                       "Do not call this tool again."}, ensure_ascii=False)

        agent = ServiceRafeeq(CONFIGS[config], m, state["mcp"], Memory(workdir / "memories" / sid.replace(":", "_")),
                              Audit(workdir / "audit.jsonl", sid), approve=ask_applicant, tracer=tracer)
        with tracer.start_as_current_span("invoke_agent Rafeeq") as span:
            span.set_attribute("gen_ai.operation.name", "invoke_agent")
            span.set_attribute("gen_ai.agent.name", "Rafeeq")
            span.set_attribute("gen_ai.conversation.id", sid)
            before = (K.LEDGER.input_tokens, K.LEDGER.output_tokens)
            try:  # K.chat already tried the fallback models; if all are down, say so instead of crashing
                run = agent.run(message, history=compact(history), shape=TracedShape(tracer, MAX_RUN_TOKENS))
            except Exception as e:  # noqa: BLE001 - rate limits, timeouts, provider errors
                span.set_attribute("error.type", type(e).__name__)
                raise HTTPException(503, "The language model is busy or unavailable. Please try again shortly.") from e
            used = {"input": K.LEDGER.input_tokens - before[0], "output": K.LEDGER.output_tokens - before[1]}
        created = []
        with connect() as con:
            for p in proposed:
                cur = con.execute("INSERT INTO confirmations (session_id, tool, args, action, details, status, created) "
                                  "VALUES (?,?,?,?,?,?,?)", (sid, p["tool"], json.dumps(p["args"], ensure_ascii=False),
                                                             p["action"], json.dumps(p["details"], ensure_ascii=False),
                                                             "awaiting", time.time()))
                created.append({"id": cur.lastrowid, "action": p["action"], "details": p["details"],
                                "status": "awaiting", "result": None})
            con.execute("INSERT OR REPLACE INTO sessions VALUES (?,?,?,?)",
                        (sid, json.dumps(run.state[1:], ensure_ascii=False, default=str),
                         spent + used["input"] + used["output"], time.time()))
        return run, used, created

    # ------------------------------------------------------------ pages
    @app.get("/", include_in_schema=False)
    def web_app() -> FileResponse:
        return FileResponse(WEB / "app.html")  # one page: #applicant (chat) and #officer (finance console)

    @app.get("/officer", include_in_schema=False)
    def officer_page() -> RedirectResponse:
        return RedirectResponse("/#officer")

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True, "model": K.MODEL, "config": config}

    # ------------------------------------------------------------ applicants
    @app.get("/v1/chat/{session_id}")
    def conversation(session_id: str, key: str = Depends(client_key)) -> dict:
        sid = session_key(key, session_id)
        rows = sql("SELECT history FROM sessions WHERE id = ?", (sid,))
        messages = []
        for msg in json.loads(rows[0]["history"]) if rows else []:
            text = msg.get("content")
            if msg.get("role") not in ("user", "assistant") or not isinstance(text, str) or not text.strip() \
                    or msg.get("tool_calls"):
                continue
            if text.startswith(EVENT):
                messages.append({"role": "event", "text": text[len(EVENT):].split("\n", 1)[0]})
            else:
                messages.append({"role": msg["role"], "text": text})
        actions = [public(r) for r in sql("SELECT * FROM confirmations WHERE session_id = ? ORDER BY id", (sid,))]
        return {"session_id": session_id, "messages": messages, "actions": actions}

    @app.post("/v1/chat", response_model=TurnOut)
    def chat(req: ChatIn, key: str = Depends(client_key)) -> TurnOut:
        with lock:
            run, used, created = run_turn(session_key(key, req.session_id), req.message)
        reply = run.final or "I couldn't finish this request. Please try again, or contact Madar on 199-00."
        return TurnOut(session_id=req.session_id, reply=reply, tools=[n for n, _ in run.tool_calls],
                       confirmations=created, tokens=used, model_steps=run.steps)

    @app.post("/v1/chat/{session_id}/confirmations/{confirmation_id}", response_model=ConfirmOut)
    def confirm(session_id: str, confirmation_id: int, body: ConfirmIn, key: str = Depends(client_key)) -> ConfirmOut:
        sid = session_key(key, session_id)
        with lock:
            rows = sql("SELECT * FROM confirmations WHERE id = ? AND session_id = ?", (confirmation_id, sid))
            if not rows:  # another session's (or key's) action is invisible, not forbidden
                raise HTTPException(404, f"No action {confirmation_id} in this conversation")
            row = rows[0]
            if row["status"] != "awaiting":
                raise HTTPException(409, f"Action {confirmation_id} is already {row['status']}")
            args = json.loads(row["args"])
            if not body.confirm:
                status, result, event = "cancelled", "cancelled by the applicant", f"You cancelled: {row['action']}"
            elif blocked := guard(row["tool"], args):  # defence in depth: the arguments are the ones shown, re-checked
                status, result, event = "blocked", blocked, f"Blocked by data policy: {row['action']}"
            else:
                result = K.call_tool(state["tools"], row["tool"], args)
                status = "failed" if result.startswith('{"error"') else "done"
                event = f"You confirmed: {row['action']}"
            audit.log("confirmation_decided", session=sid, tool=row["tool"], status=status)
            with connect() as con:
                con.execute("UPDATE confirmations SET status = ?, decided = ?, result = ? WHERE id = ?",
                            (status, time.time(), result, confirmation_id))
            row.update(status=status, result=result)
            # the agent continues: the outcome is its next turn, written by the harness (not typed by the applicant)
            note = f"{EVENT}{event}\nStatus: {status}. Tool result: {result}\nTell the applicant the outcome briefly."
            try:
                run, used, created = run_turn(sid, note)
                reply, tools, steps = run.final, [n for n, _ in run.tool_calls], run.steps
            except HTTPException as e:  # the action itself is done; only the follow-up message failed
                if e.status_code not in (429, 503):
                    raise
                reply, tools, steps, used, created = ({"done": "Done.", "cancelled": "Cancelled. Nothing was changed."}
                                                      .get(status, "This action could not be completed."), [], 0,
                                                      {"input": 0, "output": 0}, [])
        return ConfirmOut(session_id=session_id, reply=reply or "", tools=tools, confirmations=created, tokens=used,
                          model_steps=steps, action=public(row))

    # ------------------------------------------------------------ finance officers
    @app.get("/v1/officer/refunds")
    def pending_refunds(officer: str = Depends(officer_key)) -> list[dict]:
        return m.sql("SELECT r.refund_id, r.permit_id, p.type AS permit_type, r.amount_sar, r.reason, r.created_at "
                     "FROM refunds r JOIN permits p USING (permit_id) WHERE r.status = 'pending_approval' "
                     "ORDER BY r.created_at, r.refund_id")

    def decide_refund(refund_id: str, officer: str, status: str) -> dict:
        with lock:
            rows = m.sql("SELECT * FROM refunds WHERE refund_id = ?", (refund_id,))
            if not rows:
                raise HTTPException(404, f"No refund {refund_id}")
            if rows[0]["status"] != "pending_approval":
                raise HTTPException(409, f"Refund {refund_id} is {rows[0]['status']}, not waiting for approval")
            m.sql("UPDATE refunds SET status = ? WHERE refund_id = ?", (status, refund_id))
            m._audit(f"finance_{status}", {"refund_id": refund_id, "officer": short_hash(officer)})
            with connect() as con:
                con.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?)", (refund_id, rows[0]["permit_id"],
                            rows[0]["amount_sar"], status, short_hash(officer), time.time()))
        audit.log("finance_decision", refund_id=refund_id, status=status)
        note = "paid to the original payment method within 10 working days (POL-06)" if status == "approved" else \
               "not paid; the applicant may contact Madar"
        return {"refund_id": refund_id, "status": status, "note": note}

    @app.post("/v1/officer/refunds/{refund_id}/approve")
    def approve_refund(refund_id: str, officer: str = Depends(officer_key)) -> dict:
        return decide_refund(refund_id, officer, "approved")

    @app.post("/v1/officer/refunds/{refund_id}/reject")
    def reject_refund(refund_id: str, officer: str = Depends(officer_key)) -> dict:
        return decide_refund(refund_id, officer, "rejected")

    @app.get("/v1/officer/decided")
    def decided(officer: str = Depends(officer_key)) -> list[dict]:
        return sql("SELECT refund_id, permit_id, amount_sar, status, ts FROM decisions ORDER BY ts DESC LIMIT 50")

    @app.get("/v1/officer/escalations")
    def escalations(officer: str = Depends(officer_key)) -> list[dict]:
        rows = m.sql("SELECT escalation_id, permit_id, days_overdue, note, created_at FROM escalations "
                     "ORDER BY escalation_id DESC")
        return [{**r, "note": mask_text(r["note"] or "")} for r in rows]  # free text written by the model

    return app


def serve(port: int = 8793, config: str = "A") -> None:
    import uvicorn

    print(f"Rafeeq on http://127.0.0.1:{port}  ·  chat /  ·  finance officer /officer  ·  API docs /docs")
    uvicorn.run(create_app(config=config), host="127.0.0.1", port=port, log_level="warning")
