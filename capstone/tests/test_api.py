"""The HTTP API without a model: the agent's run is replaced by a scripted one that asks for write actions, so these
tests check the service around it (keys, confirmations, the guard, finance approvals, budgets, outages, pages)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from rafeeq import api
from rafeeq.kit import agentkit as K

CLIENT = {"X-API-Key": "dev-key-1"}
OFFICER = {"X-Officer-Key": "officer-key-1"}
NOURA = {"national_id_last4": "6654", "mobile": "0532236654"}
BOOKING = {"permit_id": "FTR-30311", "slot": "2026-11-05 08:00", **NOURA}
EMAIL_OK = {"permit_id": "FTR-30311", **NOURA, "subject": "Your booking", "body": "Thursday 08:00 (POL-05)."}
EMAIL_LEAKY = {**EMAIL_OK, "body": "Your mobile is 0532236654"}
OMAR_REFUND = {"permit_id": "SGN-40220", "reason": "rejected", "national_id_last4": "0093", "mobile": "0540410093"}
KHALID_REFUND = {"permit_id": "SGN-40388", "reason": "duplicate_payment", "national_id_last4": "2277",
                 "mobile": "0505502277"}
ESCALATION = {"permit_id": "BLD-20417", "note": "Overdue; call me on 0555017730", "national_id_last4": "7730",
              "mobile": "0555017730"}


def scripted(*calls):
    """A stand-in for Rafeeq.run: on a message, the 'model' asks for these tool calls through the real permission
    and confirmation; on a harness event (Confirm / Cancel), it just reports back."""

    def run(self, prompt, history=None, **kw):
        made = []
        if not prompt.startswith(api.EVENT):
            for name, args in calls:
                made.append((name, args))
                if self.permission(name, args) == "ask":
                    self._approve(name, args)
        text = "Reported the outcome." if prompt.startswith(api.EVENT) else "Please press Confirm."
        state = [{"role": "system", "content": "…"}, *(history or []), {"role": "user", "content": prompt},
                 {"role": "assistant", "content": text}]
        return K.Run(text, state, [], 1, "done", 0.0, made)

    return run


@pytest.fixture
def client(tmp_path):
    with TestClient(api.create_app(tmp_path)) as c:  # runs the lifespan: opens the MCP connection
        yield c


def chat(client, message="please do it", session="s1", headers=CLIENT):
    return client.post("/v1/chat", json={"session_id": session, "message": message}, headers=headers)


def confirm(client, cid, yes=True, session="s1", headers=CLIENT):
    return client.post(f"/v1/chat/{session}/confirmations/{cid}", json={"confirm": yes}, headers=headers)


def test_keys_are_required(client):
    assert chat(client, headers={}).status_code == 401
    assert client.get("/v1/officer/refunds", headers=CLIENT).status_code == 401  # a client key is not an officer key
    assert client.get("/v1/officer/refunds", headers=OFFICER).status_code == 200
    assert client.get("/healthz").json()["ok"]


def test_confirm_runs_the_action_and_the_agent_continues(client, monkeypatch):
    monkeypatch.setattr(api.ServiceRafeeq, "run", scripted(("book_inspection", BOOKING)))
    turn = chat(client).json()
    card = turn["confirmations"][0]
    assert card["status"] == "awaiting" and "FTR-30311" in card["action"] and "6654" not in card["action"]

    out = confirm(client, card["id"]).json()
    assert out["action"]["status"] == "done" and out["action"]["result"]["slot"] == "2026-11-05 08:00"
    assert out["reply"] == "Reported the outcome."  # the agent continued after the confirmation
    assert confirm(client, card["id"]).status_code == 409  # one decision per action

    convo = client.get("/v1/chat/s1", headers=CLIENT).json()
    assert [m["role"] for m in convo["messages"]] == ["user", "assistant", "event", "assistant"]
    assert convo["messages"][2]["text"].startswith("You confirmed: Book an inspection")


def test_cancel_changes_nothing(client, monkeypatch):
    monkeypatch.setattr(api.ServiceRafeeq, "run", scripted(("book_inspection", BOOKING)))
    card = chat(client).json()["confirmations"][0]
    out = confirm(client, card["id"], yes=False).json()
    assert out["action"]["status"] == "cancelled"
    assert client.get("/v1/chat/s1", headers=CLIENT).json()["messages"][2]["text"].startswith("You cancelled")


def test_only_the_same_conversation_can_confirm(client, monkeypatch):
    monkeypatch.setattr(api.ServiceRafeeq, "run", scripted(("book_inspection", BOOKING)))
    card = chat(client).json()["confirmations"][0]
    assert confirm(client, card["id"], session="other").status_code == 404
    assert confirm(client, card["id"], headers={"X-API-Key": "nope"}).status_code == 401


def test_guard_blocks_before_the_applicant_is_asked(client, monkeypatch):
    monkeypatch.setattr(api.ServiceRafeeq, "run", scripted(("email_applicant", EMAIL_LEAKY), ("email_applicant", EMAIL_OK)))
    cards = chat(client).json()["confirmations"]
    assert len(cards) == 1  # the leaky email never became a Confirm button
    out = confirm(client, cards[0]["id"]).json()
    assert out["action"]["status"] == "done" and out["action"]["result"]["to"] == "noura.trucks@mail.example"


def test_big_refunds_wait_for_a_finance_officer(client, monkeypatch):
    monkeypatch.setattr(api.ServiceRafeeq, "run", scripted(("create_refund", OMAR_REFUND)))
    card = chat(client).json()["confirmations"][0]
    assert confirm(client, card["id"]).json()["action"]["status"] == "finance_pending"

    queue = client.get("/v1/officer/refunds", headers=OFFICER).json()
    assert len(queue) == 1 and queue[0]["amount_sar"] == 2700 and queue[0]["permit_id"] == "SGN-40220"
    rid = queue[0]["refund_id"]
    assert client.post(f"/v1/officer/refunds/{rid}/approve", headers=OFFICER).json()["status"] == "approved"
    assert client.post(f"/v1/officer/refunds/{rid}/approve", headers=OFFICER).status_code == 409
    assert client.get("/v1/chat/s1", headers=CLIENT).json()["actions"][0]["status"] == "finance_approved"
    assert client.get("/v1/officer/decided", headers=OFFICER).json()[0]["refund_id"] == rid


def test_small_refunds_are_paid_without_an_officer(client, monkeypatch):
    monkeypatch.setattr(api.ServiceRafeeq, "run", scripted(("create_refund", KHALID_REFUND)))
    card = chat(client).json()["confirmations"][0]
    assert confirm(client, card["id"]).json()["action"]["status"] == "done"
    assert client.get("/v1/officer/refunds", headers=OFFICER).json() == []


def test_the_card_previews_the_exact_outcome_without_changing_anything(client, monkeypatch):
    monkeypatch.setattr(api.ServiceRafeeq, "run", scripted(("create_refund", OMAR_REFUND)))
    details = dict(chat(client).json()["confirmations"][0]["details"])
    assert details["Refund amount"] == "2,700 SAR" and "finance officer" in details["Then"]
    assert client.get("/v1/officer/refunds", headers=OFFICER).json() == []  # the dry run created nothing

    monkeypatch.setattr(api.ServiceRafeeq, "run", scripted(("create_refund", KHALID_REFUND)))
    details = dict(chat(client, session="khalid").json()["confirmations"][0]["details"])
    assert details["Refund amount"] == "500 SAR" and details["Then"].startswith("Paid automatically")

    monkeypatch.setattr(api.ServiceRafeeq, "run", scripted(("book_inspection", BOOKING)))
    details = dict(chat(client, session="noura").json()["confirmations"][0]["details"])
    assert details["When"] == "Thursday 5 November 2026, 08:00" and details["Inspection"] == "Health inspection"


@pytest.mark.parametrize("args", [{**BOOKING, "slot": "2026-11-03 08:00"},       # not a free slot
                                  {**BOOKING, "mobile": "0500000000"},           # identity check fails
                                  {**OMAR_REFUND, "reason": "duplicate_payment"}])  # not eligible
def test_an_action_that_would_fail_is_never_offered(client, monkeypatch, args):
    name = "create_refund" if "reason" in args else "book_inspection"
    monkeypatch.setattr(api.ServiceRafeeq, "run", scripted((name, args)))
    assert chat(client).json()["confirmations"] == []


def test_escalations_are_listed_for_the_supervisor_masked(client, monkeypatch):
    monkeypatch.setattr(api.ServiceRafeeq, "run", scripted(("escalate", ESCALATION)))
    confirm(client, chat(client).json()["confirmations"][0]["id"])
    esc = client.get("/v1/officer/escalations", headers=OFFICER).json()
    assert esc[0]["permit_id"] == "BLD-20417" and "0555017730" not in esc[0]["note"]


def test_model_outage_is_a_503_and_a_confirmed_action_still_counts(client, monkeypatch):
    monkeypatch.setattr(api.ServiceRafeeq, "run", scripted(("book_inspection", BOOKING)))
    card = chat(client).json()["confirmations"][0]

    def down(self, prompt, **kw):
        raise RuntimeError("429 rate-limited upstream")

    monkeypatch.setattr(api.ServiceRafeeq, "run", down)
    assert chat(client).status_code == 503
    out = confirm(client, card["id"]).json()  # the booking happens; only the follow-up message falls back
    assert out["action"]["status"] == "done" and out["reply"] == "Done."


def test_web_app_is_served(client):
    page = client.get("/")
    assert page.status_code == 200
    for marker in ("Which permit do you need?", "Finance approvals", "/v1/chat", "/v1/officer/refunds"):
        assert marker in page.text
    r = client.get("/officer", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/#officer"


def test_session_budget(client, monkeypatch):
    monkeypatch.setattr(api.ServiceRafeeq, "run", scripted())
    monkeypatch.setattr(api, "SESSION_TOKENS", 0)
    assert chat(client).status_code == 429
