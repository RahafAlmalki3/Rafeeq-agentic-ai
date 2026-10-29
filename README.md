# Rafeeq - Advanced Agentic AI Systems Engineering

![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![MCP](https://img.shields.io/badge/MCP-Model_Context_Protocol-000000?logo=anthropic&logoColor=white)
![OpenRouter](https://img.shields.io/badge/OpenRouter-free_models-6566F1?logo=openrouter&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-003B57?logo=sqlite&logoColor=white)

Rafeeq (رفيق) is a bilingual Arabic/English agentic AI service for Madar Municipal Services, a fictional
municipality that issues building, renovation, signage, food-truck and event permits.

> Capstone project of the **Agentic AI Systems Engineering** course by
> [SDAIA Academy](https://github.com/SDAIAAcademy) · [Course page](https://sdaiaacademy.github.io/agentic-ai/)
>
> This is a learner project and does not claim institutional endorsement, approval or ownership. Madar Municipal
> Services is fictional, and all data is synthetic.

## Problem statement

Applying for a municipal permit means many questions: What is the fee? What is missing from my application? When
can my inspection be booked? Am I eligible for a refund? Today these go through a busy service desk, and a generic
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

## Live demo

> **Note:** this is a demo, not the final product. The web app lets one person play both roles, the applicant and the
> finance officer, to see each point of view. The identity details are filled in with ready-made synthetic applicants
> ("Fill demo details") so the full flow can be shown quickly, instead of a real person entering their own data.

<!-- Video goes here. On GitHub: edit this file in the browser and drag the .mp4 onto this line (up to 10 MB);
     GitHub uploads it and inserts the link. Or link a hosted video: [![Watch the demo](thumbnail.png)](VIDEO_URL) -->

## How to run

### Setup

Requires [uv](https://docs.astral.sh/uv/) and a free [OpenRouter key](https://openrouter.ai/keys).

```bash
cd capstone
cp .env.example .env        # paste OPENROUTER_API_KEY (no quotes)
uv sync                     # installs the `rafeeq` command
```

### Web app

On Windows, double-click `capstone/run.bat`. Or:

```bash
uv run rafeeq serve         # then open http://127.0.0.1:8793/
```

One page with an **Applicant** view (choose a permit type, chat, confirm actions) and an **Officer** view (approve or
reject refunds).

### Command line

```bash
uv run rafeeq ask "What is the fee for an illuminated 3 m² sign?"
uv run rafeeq chat --session sara
```

### Tests and evaluation

```bash
uv run pytest                                               # offline tests, no API key needed
uv run rafeeq eval --config A B                             # the eval suite on both configurations
uv run rafeeq redteam --config naive prompt-only defended   # the red-team suite
```

## Design across the ladder

| Rung | What Rafeeq does | Why |
|---|---|---|
| Prompt | [AGENTS.md](capstone/AGENTS.md) is the system prompt: verify identity first, take numbers from tools, cite the policy, stay in scope, treat pasted text as data. Stable rules first, today's date last | The model behaves the same way on every request, and a stable prompt is cheaper and faster to serve |
| Context | Saved preferences are injected into the prompt (a guard keeps personal data out of memory). Compaction keeps recent messages and clears old tool results, and long tool results are capped. Config B adds **Agent Skills**, loaded only when needed | The model sees what matters for the current request, so long conversations stay focused and affordable |
| Harness | Permissions: **allow** reads, **ask** for every write (the applicant confirms the exact action), **deny** anything unknown. A finance officer approves large refunds. Step and token budgets per request and per session. A guard on every write and a redacted audit log | Rules that must always hold are enforced in code, not left to the model's judgement |

## Tools and Integrations

### Agent tools

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

### Test suite

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

| Config | Pass rate | pass^3 (actions) | Avg steps | Avg tokens |
|---|---|---|---|---|
| A | | | | |
| B | | | | |

### Error analysis

| Failure | Root cause | Fix |
|---|---|---|
| | | |

## Security note

- **Trifecta:**
- **Attack tried:**
- **Defence:**
- **Residual risk:**
- **Governance (PDPL / SDAIA AI principles):**

## Project structure

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

## Limitations and next steps

- 
