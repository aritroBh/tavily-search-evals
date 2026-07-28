#!/usr/bin/env python3
"""SimpleQA runner for the SeaWeb search API (api.seaweb.tech, anonymous MCP).

Mirrors this repo's protocol so numbers are comparable to its other
providers, with every deviation pinned and disclosed:

  search    SeaWeb `search_web` over MCP HTTP (anonymous tier), top 10 docs,
            formatted exactly like base_handler._format_search_results_for_prompt.
  compose   THIS REPO'S PostProcessor extraction prompt, verbatim, in
            --mode parity. --mode honest appends an explicit abstention
            instruction (SeaWeb's product behavior; SimpleQA's grader has a
            NOT_ATTEMPTED class and its F-score rewards calibrated
            abstention, so both modes are legitimate — report both).
  grade     The official SimpleQA grader template (same one
            evaluators/correctness_evaluator.py embeds), A/B/C letter out.
  models    DEVIATION from upstream (which uses GPT-4.1 via OpenAI):
            composer + grader run on Claude (headless CLI, pinned via
            --claude-model, default claude-haiku-4-5-20251001) because this
            machine has no OpenAI key. Rerun with upstream's runner once an
            OPENAI_API_KEY exists to get strict parity.

Usage:
  python3 seaweb_run.py --slice geography --limit 12 --mode parity --out var/smoke
  python3 seaweb_run.py --slice geography --mode parity --out var/geo_parity
  python3 seaweb_run.py --slice all --random-sample 200 --mode honest --out var/all_honest

Metrics reported (SimpleQA standard): correct / incorrect / not_attempted,
attempted rate, accuracy_given_attempted, F = harmonic mean of overall
correct and accuracy_given_attempted.
"""
from __future__ import annotations

import argparse
import ast
import csv
import json
import random
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parent
DATASET = REPO / "datasets" / "simple_qa_test_set.csv"
CLAUDE_BIN = "/Users/aritro/.local/bin/claude"  # NEVER bare `claude`: zsh shadow routes to Ollama
MCP_URL = "https://api.seaweb.tech/mcp"
SEARCH_GAP_S = 2.1  # anonymous tier allows 30 req / 60 s
MAX_DOCS = 10       # upstream config.json: max_results 10

# Upstream PostProcessor documents-mode prompt, verbatim (utils/post_processor.py).
PARITY_PROMPT = """
    You are an advanced assistant operating in strict extraction mode.
    Your mission is **extremely important**: extract **only** the **direct, final answer** to the user's query, based solely on the provided list of documents. Each document includes a `URL` and `Content`.

    ## Rules (non-negotiable):
    - Do **not** explain, paraphrase, summarize, or add any context.
    - Return **only** the final answer — nothing else.
    - If multiple documents suggest different answers, choose the one from the **most reliable URL** (e.g., Wikipedia, .gov, .edu, official sources).

    ## Query:
    {query}

    ## Documents list:
    {docs}

    Now return the single, most accurate answer to the query.
"""

HONEST_SUFFIX = """
    ## Additional rule:
    - If the documents do not contain the information needed to answer, reply exactly: I cannot answer.
"""

GRADER_TAIL = """
    Here is a new example. Simply reply with either CORRECT, INCORRECT, NOT ATTEMPTED. Don't apologize or correct yourself if there was a mistake; we are just trying to grade the answer.
    ```
    Question: {question}
    Gold target: {target}
    Predicted answer: {predicted}
    ```

    Grade the predicted answer of this new question as one of:
    A: CORRECT
    B: INCORRECT
    C: NOT_ATTEMPTED

    Just return the letters "A", "B", or "C", with no text around it.
"""


def grader_template() -> str:
    """Pull the official grader body out of this repo's evaluator so the two
    runners can never drift, then append the standard A/B/C tail."""
    src = (REPO / "evaluators" / "correctness_evaluator.py").read_text()
    m = re.search(r'OPENAI_GRADER_TEMPLATE = """(.*?)"""', src, re.S)
    if not m:
        raise RuntimeError("grader template not found in correctness_evaluator.py")
    return m.group(1) + GRADER_TAIL


def load_questions(slice_name: str, limit: int | None, sample: int | None, seed: int) -> list[dict]:
    rows = []
    with open(DATASET, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            meta = ast.literal_eval(r["metadata"])
            rows.append({"q": r["problem"], "gold": r["answer"], "topic": meta.get("topic", "")})
    if slice_name == "geography":
        rows = [r for r in rows if r["topic"] == "Geography"]
    elif slice_name != "all":
        raise SystemExit(f"unknown slice {slice_name!r}")
    if sample:
        rows = random.Random(seed).sample(rows, min(sample, len(rows)))
    if limit:
        rows = rows[:limit]
    return rows


class SeaWebClient:
    def __init__(self) -> None:
        self._client = httpx.Client(timeout=30)
        self._sid: str | None = None
        self._lock = threading.Lock()
        self._last = 0.0
        self._init()

    def _init(self) -> None:
        r = self._client.post(
            MCP_URL,
            headers=self._headers(),
            json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                           "clientInfo": {"name": "seaweb-simpleqa", "version": "1"}},
            },
        )
        r.raise_for_status()
        self._sid = r.headers.get("mcp-session-id")

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self._sid:
            h["mcp-session-id"] = self._sid
        return h

    def search(self, query: str) -> list[tuple[str, str]]:
        """Return up to MAX_DOCS (url, content) tuples. Paced to the anon limit."""
        with self._lock:
            wait = SEARCH_GAP_S - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
        for attempt in range(3):
            r = self._client.post(
                MCP_URL, headers=self._headers(),
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                      "params": {"name": "search_web", "arguments": {"query": query}}},
            )
            if r.status_code == 429:
                time.sleep(10 * (attempt + 1))
                continue
            r.raise_for_status()
            break
        body = r.text
        try:
            if body.lstrip().startswith("{"):
                payload = json.loads(body)
            else:  # SSE framing: take the last `data:` line
                data_lines = [l[5:].strip() for l in body.splitlines() if l.startswith("data:")]
                payload = json.loads(data_lines[-1])
            text = payload["result"]["content"][0]["text"]
            rows = json.loads(text)
        except Exception:
            return []
        docs = []
        for row in rows[:MAX_DOCS]:
            url, content = row.get("url", ""), row.get("text", "")
            if url and content:
                docs.append((url, content))
        return docs


def format_docs(docs: list[tuple[str, str]]) -> str:
    # byte-identical to base_handler._format_search_results_for_prompt
    return "\n".join(
        f"\n**Document {i + 1}.** Source: {url}\nContent: {content}"
        for i, (url, content) in enumerate(docs)
    )


def claude(prompt: str, model: str) -> str:
    """One headless Claude call. Isolated from user settings/hooks/MCP."""
    for attempt in range(2):
        try:
            out = subprocess.run(
                [CLAUDE_BIN, "--model", model, "--setting-sources", "",
                 "--strict-mcp-config", "-p", prompt],
                capture_output=True, text=True, timeout=180,
            )
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except subprocess.TimeoutExpired:
            pass
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slice", default="geography", choices=["geography", "all"])
    ap.add_argument("--mode", default="parity", choices=["parity", "honest"])
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--random-sample", type=int, default=None)
    ap.add_argument("--seed", type=int, default=20260728)
    ap.add_argument("--claude-model", default="claude-haiku-4-5-20251001")
    ap.add_argument("--llm-workers", type=int, default=4)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows_path = out_dir / "rows.jsonl"
    done = set()
    if rows_path.exists():  # resume: skip already-graded questions
        for line in rows_path.open():
            try:
                done.add(json.loads(line)["q"])
            except Exception:
                pass

    questions = load_questions(args.slice, args.limit, args.random_sample, args.seed)
    todo = [r for r in questions if r["q"] not in done]
    print(f"slice={args.slice} mode={args.mode} total={len(questions)} todo={len(todo)}")

    grader = grader_template()
    compose_prompt = PARITY_PROMPT + (HONEST_SUFFIX if args.mode == "honest" else "")
    client = SeaWebClient()
    write_lock = threading.Lock()

    def compose_and_grade(item: dict, docs: list[tuple[str, str]]) -> None:
        if docs:
            predicted = claude(
                compose_prompt.replace("{query}", item["q"]).replace("{docs}", format_docs(docs)),
                args.claude_model,
            ) or "I cannot answer."
        else:
            predicted = "I cannot answer."  # empty result = abstention, both modes
        if predicted.strip().rstrip(".").lower() == "i cannot answer":
            grade = "NOT_ATTEMPTED"  # deterministic; no grader call needed
        else:
            letter = claude(
                grader.replace("{question}", item["q"]).replace("{target}", item["gold"])
                .replace("{predicted}", predicted),
                args.claude_model,
            ).strip().upper()[:1]
            grade = {"A": "CORRECT", "B": "INCORRECT", "C": "NOT_ATTEMPTED"}.get(letter, "GRADE_ERROR")
        rec = {"q": item["q"], "gold": item["gold"], "topic": item["topic"],
               "n_docs": len(docs), "urls": [u for u, _ in docs[:3]],
               "predicted": predicted, "grade": grade}
        with write_lock:
            with rows_path.open("a") as f:
                f.write(json.dumps(rec) + "\n")

    with ThreadPoolExecutor(max_workers=args.llm_workers) as pool:
        futures = []
        for i, item in enumerate(todo):
            docs = client.search(item["q"])  # sequential + paced
            futures.append(pool.submit(compose_and_grade, item, docs))
            if (i + 1) % 25 == 0:
                print(f"searched {i + 1}/{len(todo)}")
        for f in futures:
            f.result()

    # summarize everything on disk (including prior resumed rows)
    recs = [json.loads(l) for l in rows_path.open()]
    n = len(recs)
    c = sum(r["grade"] == "CORRECT" for r in recs)
    i_ = sum(r["grade"] == "INCORRECT" for r in recs)
    na = sum(r["grade"] == "NOT_ATTEMPTED" for r in recs)
    ge = sum(r["grade"] == "GRADE_ERROR" for r in recs)
    attempted = c + i_
    acc_att = c / attempted if attempted else 0.0
    overall = c / n if n else 0.0
    f1 = (2 * acc_att * overall / (acc_att + overall)) if (acc_att + overall) else 0.0
    summary = {
        "slice": args.slice, "mode": args.mode, "n": n,
        "correct": c, "incorrect": i_, "not_attempted": na, "grade_error": ge,
        "attempted_rate": round(attempted / n, 4) if n else 0,
        "accuracy_given_attempted": round(acc_att, 4),
        "overall_correct": round(overall, 4),
        "f_score": round(f1, 4),
        "search": "seaweb prod anon MCP, top10",
        "composer_grader_model": args.claude_model,
        "composer_prompt": "upstream PostProcessor verbatim" + (" + abstention rule" if args.mode == "honest" else ""),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
