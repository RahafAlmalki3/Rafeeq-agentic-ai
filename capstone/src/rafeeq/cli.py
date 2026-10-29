"""The `rafeeq` command.

    uv run rafeeq ask "What is the fee for an illuminated 3 m² sign?"
    uv run rafeeq chat --session sara              # a conversation, kept in artifacts/rafeeq/
    uv run rafeeq eval --config A B                # evals/tasks.jsonl on two configurations → evals/results/
    uv run rafeeq eval --config A --limit 5 --min-rate 0.6    # a smoke run that fails below a pass rate (CI)
    uv run rafeeq redteam --config naive prompt-only defended
    uv run rafeeq context --session sara           # token anatomy of a session, before/after compaction
    uv run rafeeq serve --port 8793                # the HTTP API (see api.py)
    uv run rafeeq prepare                          # (re)generate data/madar.sqlite and data/inbox.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import mcp_server, redteam
from .agent import CONFIGS, MAX_SESSION_TOKENS, OUT, Rafeeq, compact, eval_agent, load_skill
from .guards import Audit, approve_all, approve_cli
from .kit import agentkit as K
from .kit import evals, madar, prepare_data
from .memory import Memory

TASKS = K.ROOT / "evals" / "tasks.jsonl"
RESULTS = K.ROOT / "evals" / "results"  # committed: the submission includes the eval CSVs


def context_report(history: list) -> None:
    tool_defs = [K.tool_schema(t) for t in [*madar.Madar().tools(), load_skill, Memory(OUT / "_probe").tool()]]
    print(f"system prompt (A)     {K.count_tokens(CONFIGS['A']['system']):>7,} tokens")
    print(f"system prompt (B)     {K.count_tokens(CONFIGS['B']['system']):>7,} tokens")
    print(f"tool definitions      {K.count_tokens(tool_defs):>7,} tokens ({len(tool_defs)} tools)")
    print(f"history: raw          {K.count_tokens(history):>7,} tokens ({len(history)} messages)")
    c = compact(history)
    print(f"history: compacted    {K.count_tokens(c):>7,} tokens ({len(c)} messages)")


def run_eval(args) -> int:
    import pandas as pd

    tasks = evals.load_tasks(args.tasks, ids=args.ids)[: args.limit]
    RESULTS.mkdir(parents=True, exist_ok=True)
    frames = []
    for name in args.config:
        tokens: list[int] = []
        df = evals.run_suite(eval_agent(CONFIGS[name], name, tokens), tasks, trials=args.trials, name=name)
        df["tokens"] = tokens
        df.drop(columns="final").to_csv(RESULTS / f"eval_{name}.csv", index=False)
        df.to_json(RESULTS / f"eval_{name}_answers.jsonl", orient="records", lines=True, force_ascii=False)
        frames.append(df)
    df = pd.concat(frames)
    overall, _ = evals.report(df)
    overall["avg_steps"] = df.groupby("agent").steps.mean().round(1)
    overall["avg_tokens"] = df.groupby("agent").tokens.mean().round(0)
    print("\n", overall, "\n\nby category:\n", df.pivot_table(index="category", columns="agent", values="passed").round(2))
    print(f"\nresults in {RESULTS} · {K.LEDGER}")
    rate = float(df.passed.mean()) if len(df) else 0.0
    if args.min_rate is not None and rate < args.min_rate:
        print(f"❌ pass rate {rate:.2f} is below the gate {args.min_rate}")
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="rafeeq", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("ask", help="one request; you confirm write actions in the terminal")
    a.add_argument("question")
    a.add_argument("--config", default="A", choices=CONFIGS)
    a.add_argument("--yes", action="store_true", help="confirm every write without asking (scripted demos)")
    c = sub.add_parser("chat", help="a conversation with memory and compaction")
    c.add_argument("--session", default="default")
    c.add_argument("--config", default="A", choices=CONFIGS)
    e = sub.add_parser("eval", help="run the eval suite on one or more configurations")
    e.add_argument("--tasks", type=Path, default=TASKS)
    e.add_argument("--config", nargs="+", default=["A", "B"], choices=CONFIGS)
    e.add_argument("--trials", type=int, default=1)
    e.add_argument("--ids", nargs="*", help="run only these task IDs")
    e.add_argument("--limit", type=int, help="run only the first N tasks (develop on a few, run all once)")
    e.add_argument("--min-rate", type=float, help="exit 1 if the pass rate is below this (CI gate)")
    r = sub.add_parser("redteam", help="attacks and benign requests against three builds")
    r.add_argument("--config", nargs="+", default=["naive", "defended"], choices=["naive", "prompt-only", "defended"])
    x = sub.add_parser("context", help="token anatomy of a chat session")
    x.add_argument("--session", default="default")
    s = sub.add_parser("serve", help="the web chat, finance console and HTTP API, with traces")
    s.add_argument("--port", type=int, default=8793)
    s.add_argument("--config", default="A", choices=CONFIGS)
    sub.add_parser("prepare", help="(re)generate the synthetic Madar database and inbox")
    args = ap.parse_args()

    if args.cmd == "ask":
        m = madar.Madar(OUT / "db")
        with K.MCPBridge(mcp_server.build(m)) as mcp:
            agent = Rafeeq(CONFIGS[args.config], m, mcp, Memory(OUT / "memories" / "default"),
                           Audit(OUT / "audit.jsonl", "ask"), approve=approve_all if args.yes else approve_cli)
            run = agent.run(args.question, verbose=True)
        print("\n" + (run.final or f"(stopped: {run.stopped})"))
        print(f"\n· {run.steps} steps · {K.LEDGER}")
    elif args.cmd == "chat":
        path = OUT / f"session-{args.session}.json"
        history = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        m = madar.Madar(OUT / f"db-{args.session}")
        with K.MCPBridge(mcp_server.build(m)) as mcp:
            agent = Rafeeq(CONFIGS[args.config], m, mcp, Memory(OUT / "memories" / args.session),
                           Audit(OUT / "audit.jsonl", args.session))
            print("Type your message (empty line to quit).")
            while (msg := input("\n🧑 ").strip()):
                if K.LEDGER.input_tokens + K.LEDGER.output_tokens > MAX_SESSION_TOKENS:
                    print("🤖 This session has used its budget. Please start a new session.")
                    break
                run = agent.run(msg, history=compact(history))
                print("🤖", run.final or f"(stopped: {run.stopped})")
                history = run.state[1:]
                path.write_text(json.dumps(history, ensure_ascii=False, default=str), encoding="utf-8")
    elif args.cmd == "eval":
        return run_eval(args)
    elif args.cmd == "redteam":
        RESULTS.mkdir(parents=True, exist_ok=True)
        redteam.run(args.config, RESULTS)
    elif args.cmd == "context":
        path = OUT / f"session-{args.session}.json"
        context_report(json.loads(path.read_text(encoding="utf-8")) if path.exists() else [])
    elif args.cmd == "serve":
        from .api import serve  # FastAPI and OpenTelemetry load only for the service

        serve(args.port, args.config)
    else:
        prepare_data.build()
    return 0


if __name__ == "__main__":
    sys.exit(main())
