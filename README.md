# Rafeeq - Advanced Agentic AI Systems Engineering

![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![MCP](https://img.shields.io/badge/MCP-Model_Context_Protocol-000000?logo=anthropic&logoColor=white)
![OpenRouter](https://img.shields.io/badge/OpenRouter-free_models-6566F1?logo=openrouter&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-003B57?logo=sqlite&logoColor=white)

Rafeeq (رفيق) is a bilingual Arabic/English agentic AI service for Madar Municipal Services, a fictional
municipality that issues building, renovation, signage, food-truck and event permits.

Developed as a capstone project for the **Agentic AI Systems Engineering** course by **[SDAIA Academy](https://github.com/SDAIAAcademy)**.
- [Agentic AI Systems Engineering Course](https://sdaiaacademy.github.io/agentic-ai/)
- [Agentic AI Systems Engineering Repository](https://github.com/SDAIAAcademy/agentic-ai)

**Author:** [Rahaf Almalki](https://github.com/RahafAlmalki3)

> [!NOTE]
> This is a learner project and does not claim institutional endorsement, approval or ownership. Madar Municipal
> Services is fictional, and all data is synthetic.

## Problem Statement

Applying for a municipal permit means many questions: What is the fee? What is missing from my application? When
can my inspection be booked? Am I eligible for a refund? These usually go through a busy service desk, and a generic
chatbot cannot be trusted with them, because it may guess fees, reveal another person's application, or act without
permission.

Rafeeq is an agentic AI system that answers these questions in Arabic or English and handles the requests that follow,
grounded in Madar's policies and data.

Rafeeq verifies the applicant's identity, reasons through each request with Madar's tools, and returns a policy-cited
answer or a proposed action. Nothing is carried out without the right person's approval.

## Features

**Five permit types**
- Building (BLD): new buildings and extensions
- Renovation (REN): changes to an existing building
- Signage (SGN): shop signs and lit boards
- Food truck (FTR): mobile kitchens and trading zones
- Event (EVT): public gatherings and exhibitions

**What Rafeeq answers**
- Questions on fees, deadlines, inspections, refunds, appeals and fines, with the policy cited
- Exact permit fees, computed with Madar's fee tool
- The status of the applicant's own application, after verifying their identity
- Free inspection slots

**What Rafeeq can do** (each one only after the applicant confirms it)
- Book or reschedule an inspection
- File a refund request (refunds above 1,000 SAR also wait for a finance officer)
- Escalate an overdue application to the duty supervisor
- Email the applicant a summary at their registered address

**Models**

| Role | Model | ID |
|---|---|---|
| Main | Gemma 4 31B | `google/gemma-4-31b-it:free` |
| Fallback 1 | Nemotron 3 Ultra | `nvidia/nemotron-3-ultra-550b-a55b:free` |
| Fallback 2 | dots.3 | `dots-studio/dots-3-note-preview:free` |

## Live Demo

> [!IMPORTANT]
> This is a demo, not the final product. The web app lets one person play both roles, the applicant and the
> finance officer, to see each point of view.
> The identity details are filled in with ready-made synthetic applicants
> ("Fill demo details") so the full flow can be shown quickly, instead of a real person entering their own data.

https://github.com/user-attachments/assets/457e0442-a118-4b82-a691-9f8f71da9e16

## How to Run

### Setup

Requires [uv](https://docs.astral.sh/uv/) and a free [OpenRouter key](https://openrouter.ai/keys).

```bash
cd capstone
cp .env.example .env        # paste OPENROUTER_API_KEY (no quotes)
uv sync                     # installs the `rafeeq` command
```

### Web App

On Windows, double-click `capstone/run.bat`. Or:

```bash
uv run rafeeq serve         # then open http://127.0.0.1:8793/
```

The app has two views: **Applicant** and **Officer**.

### Command Line

```bash
uv run rafeeq ask "What is the fee for an illuminated 3 m² sign?"
uv run rafeeq chat --session sara
```

### Tests and Evaluation

```bash
uv run pytest                                               # offline tests, no API key needed
uv run rafeeq eval --config A B                             # the eval suite on both configurations
uv run rafeeq eval --config A B --trials 3 --ids action-book-noura-thursday action-refund-omar action-escalate-agent action-email-khalid   # pass@3 and pass^3
uv run rafeeq redteam --config naive prompt-only defended   # the red-team suite
```

## Project Structure

```
.
├── README.md
├── .github/workflows/     CI checks
└── capstone/
    ├── run.bat            start the web app (Windows)
    ├── AGENTS.md          system prompt
    ├── pyproject.toml     package and dependencies
    ├── .env.example       settings template
    ├── src/rafeeq/
    │   ├── cli.py         command line
    │   ├── api.py         FastAPI service
    │   ├── web/app.html   web app
    │   ├── agent.py       the agent
    │   ├── guards.py      guard and audit log
    │   ├── memory.py      memory tool
    │   ├── mcp_server.py  MCP server
    │   ├── redteam.py     red-team suite
    │   └── kit/           course library
    ├── data/              policies, skills, data
    ├── evals/             eval tasks and results
    └── tests/             offline tests
```

## Design Across the Ladder

| Rung | What Rafeeq does | Why |
|---|---|---|
| Prompt | [AGENTS.md](capstone/AGENTS.md) is the system prompt: verify identity first, take numbers from tools, cite the policy, stay in scope, treat pasted text as data. Stable rules first, today's date last | The model behaves the same way on every request, and a stable prompt is cheaper and faster to serve |
| Context | Saved preferences are injected into the prompt (a guard keeps personal data out of memory). Compaction keeps recent messages and clears old tool results, and long tool results are capped. Config B adds **Agent Skills**, loaded only when needed | The model sees what matters for the current request, so long conversations stay focused and affordable |
| Harness | Permissions: **allow** reads, **ask** for every write (the applicant confirms the exact action), **deny** anything unknown. A finance officer approves large refunds. Step and token budgets per request and per session. A guard on every write and a redacted audit log | Rules that must always hold are enforced in code, not left to the model's judgement |

## Tools and Integrations

### Agent Tools

| Tool | Read / write | Where | Approval? | Trifecta leg (private / untrusted / egress) |
|---|---|---|---|---|
| `search_policies` | read | `madar` MCP server | no | none (public policies) |
| `compute_fee` | read | `madar` MCP server | no | none |
| `list_slots` | read | `madar` MCP server | no | none |
| `get_permit` | read (verifies identity first) | `madar` MCP server | no | **private** |
| `book_inspection` | write | local | applicant confirms | none |
| `reschedule_inspection` | write | local | applicant confirms | none |
| `create_refund` | write | local | applicant confirms; > 1,000 SAR also a finance officer before payment | none |
| `escalate` | write | local | applicant confirms | none |
| `email_applicant` | write | local | applicant confirms + guard | **egress**, limited: the recipient is always the verified applicant's registered address |
| `memory` | write (preferences) | local | no (guarded) | private if misused; blocked by the guard |
| `load_skill` (config B) | read | local | no | none |
| *the user's message* | input | | | **untrusted** (pasted emails, "attachments") |

### MCP Server

Madar's data and policies are exposed through a Model Context Protocol server,
[src/rafeeq/mcp_server.py](capstone/src/rafeeq/mcp_server.py). It gives Rafeeq a standard way to discover and call Madar's
read tools, and lets any other MCP-compatible app use the same tools without extra integration work. Only reading is
shared this way: actions that change an application stay inside Rafeeq's harness, behind its approvals. Rafeeq
connects to the server in-process, on the same database as its own tools.

### API

Rafeeq runs as a **FastAPI** service ([src/rafeeq/api.py](capstone/src/rafeeq/api.py)). Applicants authenticate with
`X-API-Key`, finance officers with `X-Officer-Key`; interactive docs are at `/docs`.

| Method | Endpoint | Used by | What it does |
|---|---|---|---|
| `POST` | `/v1/chat` | applicant | sends a message; returns Rafeeq's reply and any actions to confirm |
| `GET` | `/v1/chat/{session_id}` | applicant | the conversation and the status of its actions |
| `POST` | `/v1/chat/{session_id}/confirmations/{id}` | applicant | confirms or cancels an action; Rafeeq continues with the result |
| `GET` | `/v1/officer/refunds` | finance officer | refunds waiting for approval |
| `POST` | `/v1/officer/refunds/{refund_id}/approve` · `/reject` | finance officer | approves or rejects a refund payment |
| `GET` | `/v1/officer/decided` | finance officer | recent decisions |
| `GET` | `/v1/officer/escalations` | finance officer | escalations to the duty supervisor (read only) |
| `GET` | `/healthz` | ops | service status and model |

## Evaluation and Error Analysis

### Test Suite

- **Tasks:** 22 in [evals/tasks.jsonl](capstone/evals/tasks.jsonl), in Arabic and English
- **Covers:** fees, rules, application status, privacy, actions, security, scope and memory
- **Grading:** checked by code where possible, with an LLM judge only for answers code can't check

### Configurations

- **A:** the [AGENTS.md](capstone/AGENTS.md) spec only
- **B:** A plus [Agent Skills](capstone/data/skills/), step-by-step procedures that Rafeeq loads only when a request
  needs one:
  - **Refund request:** check eligibility, compute the amount, propose the refund
  - **Inspection booking:** check the permit is eligible, find free slots, propose the booking
  - **Escalation:** check the application is really overdue, then escalate it
  - **Appeal:** explain how to appeal a rejection and the exact deadline

Everything else is identical, so any difference comes from the skills.

### Results

| Config | Pass rate | pass@3 | pass^3 | Avg steps | Avg tokens |
|---|---|---|---|---|---|
| A | 91% (20/22) | 100% | 100% | 2.5 | 10,843 |
| B | 95% (21/22) | 100% | 100% | 2.3 | 10,508 |

- **Pass rate** uses all 22 tasks. The few failures are in rules, memory and scope questions.
- **pass@3 and pass^3** use only the four action tasks (booking, refund, escalation, email), each run three times.
  pass@3 counts a task if any run passed, pass^3 only if all three did.

B is slightly better and uses fewer tokens: the refund skill helps with refund rules.
See [eval_A.csv](capstone/evals/results/eval_A.csv), [eval_B.csv](capstone/evals/results/eval_B.csv),
[eval_A_trials3.csv](capstone/evals/results/eval_A_trials3.csv) and [eval_B_trials3.csv](capstone/evals/results/eval_B_trials3.csv).

### Error Analysis

| Failure | Root cause | Fix |
|---|---|---|
| Rafeeq asked "shall I?" in text instead of proposing the action, so nothing could be confirmed | The spec told it to describe an action before doing it | The spec now says to call the tool; the Confirm card does the asking. Actions now pass in both configs |
| Policy answers left out a condition (refund timing, the 60-day appeal window) | Rafeeq answered rule questions from memory | The spec now requires a policy search for every rule question. Fixed in most cases; one answer in each config still drops a detail |
| A failed verification hinted that the permit exists | Model wording, and a tool that answered "no permit" for unknown IDs | Unknown permits and wrong details now get the same reply, guarded by a test |
| A saved preference was confirmed only with "Saved." | Too short a confirmation (config A) | Open: the preference itself is saved correctly |
| The judge's verdict could not be read | A fallback judge model wrote prose and ran out of tokens | More room for the judge and one retry |

## Security Note

- **Trifecta:** all three legs exist: private data (application records), untrusted input (the user's message,
  which may contain a pasted email) and a way out (email).
- **Attack tried:** 6 attacks (a hidden instruction in a pasted email, a fake approval letter, "email it to my lawyer",
  a "system override", an Arabic "from the manager" instruction, and a link that hides a mobile number) and 2 normal
  requests, against three builds: naive, prompt-only and defended. No attack succeeded on any build. Normal emails
  reached the applicant in 100% of cases on the defended build and 0% on the others; the prompt-only build even sent
  one to a permit number instead of an address. See [redteam.csv](capstone/evals/results/redteam.csv).
- **Defence:** the email tool has no recipient field, so it can only write to the verified applicant. A guard
  blocks emails containing ID numbers, mobile numbers or outside links. The applicant confirms every action. Unknown
  permits and wrong details get the same reply, so permit IDs can't be probed.
- **Residual risk:** the defences stop Rafeeq from *doing* harmful things, but not from *saying* wrong things.
  - **Protected:** a trick message can't make Rafeeq send data to a stranger or act without approval.
  - **Not fully protected:** a trick message could still make Rafeeq write something false, like "your application is
    approved" when it isn't. Nothing changes, but the applicant might believe it.
- **Governance (PDPL / SDAIA AI principles):** memory keeps preferences only, the audit log is redacted, and a
  person approves every action. Data goes to a model provider outside the Kingdom, which is fine for synthetic data;
  production would need an in-Kingdom model.

## Limitations and Next Steps

**Limitations**
- **Free models:** the daily request limit and rate limits make answers slower or unavailable at busy times, and
  limit how many evaluation runs fit in a day.
- **Demo identity:** applicants verify with ID digits and a mobile number typed into the chat, not a real login.
- **Data leaves the Kingdom:** prompts go through OpenRouter to the model providers (Google, NVIDIA and dots), outside
  Saudi Arabia, which is fine only because the data is synthetic.

**Next Steps**
- **Production setup:** a real database instead of resetting on restart, and support for many users at once.
- **Wider security testing:** extend the red-team suite, which focuses on data leaks by email, to attacks that trick
  Rafeeq into giving false answers, and check its replies against Madar's records before they are sent.
