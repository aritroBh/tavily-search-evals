#!/usr/bin/env python3
"""SimpleQA runner for SeaWeb's search over a LOCAL frozen pages_artifact
(sfblogs_20260727), for the travel-anchored slice (TODOS.md #97).

Reuses seaweb_run.py's protocol verbatim (imported, not copied) so numbers
stay comparable to that script's other runs: PARITY_PROMPT / HONEST_SUFFIX
compose prompts, grader_template() (pulled live from
evaluators/correctness_evaluator.py so the two runners can't drift), the
`claude()` headless-CLI caller, and the summary-statistics formula.

WHY LOCAL INSTEAD OF PROD MCP: bulk search runs the exact serving ranking
code (SeaWeb/gateway/index_search.py::search_passages, fielded re-rank +
coordination-filter honesty gates, byte-identical to what gateway/server.py's
search_web tool calls) directly against the frozen artifact SeaWeb/var/
pages_artifact/pending/sfblogs_20260727/, in-process — no MCP round trip, no
30-req/60s anon rate limit, no per-query 2.1s pacing. Per SeaWeb's own
2026-07-28 pipeline note, sfblogs_20260727 IS what prod is currently serving
(rolled back there after a regression), so this is not a proxy for prod, it
is prod's ranking logic over prod's current bytes, run locally. A separate
script (prod_parity_check.py) spot-checks a handful of these same queries
against the live api.seaweb.tech MCP endpoint to prove that equivalence
empirically rather than asserting it.

DEVIATION DISCLOSED: gateway/server.py's search_web tool wraps
search_passages()'s return in an envelope, {"coverage":..., "results": [...],
"note": ...} — a change dated 2026-07-28 in that file's own comments. This
script calls search_passages() directly, in-process, so it receives the bare
list[dict] search_passages() actually returns — no envelope exists at this
layer to begin with. See prod_parity_check.py for how the MCP-JSON-RPC path
(which DOES carry the envelope) is parsed for the prod spot-check.

Usage:
  python3 seaweb_run_local.py --mode honest --out results/20260730-simpleqa-travel/local_honest
  python3 seaweb_run_local.py --mode parity --out results/20260730-simpleqa-travel/local_parity
  python3 seaweb_run_local.py --mode honest --limit 5 --out var/smoke   # smoke test
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parent
SEAWEB_ROOT = Path("/Users/aritro/Downloads/Start up/SeaWeb")
ARTIFACT_DIR = Path(os.environ["SEAWEB_EVAL_ARTIFACT_DIR"]) if os.environ.get("SEAWEB_EVAL_ARTIFACT_DIR") else SEAWEB_ROOT / "var" / "pages_artifact" / "pending" / "sfblogs_20260727"
SLICE_PATH = REPO / "datasets" / "travel_slice_20260730.jsonl"
MAX_DOCS = 10  # matches seaweb_run.py's MAX_DOCS / upstream config.json max_results

sys.path.insert(0, str(REPO))
import seaweb_run  # noqa: E402 — reused for prompts / claude() / grader_template()

# Point SeaWeb's serving-side index reader at the frozen local artifact
# BEFORE first import. index_serve reads SEAWEB_INDEX_DIR live on every call
# (index_store.resolve_index_dir()), so this line, executed before any
# search, is sufficient — no external env var / subshell required.
os.environ["SEAWEB_INDEX_DIR"] = str(ARTIFACT_DIR)
os.environ.setdefault("SEAWEB_INDEX_VERIFY_ASYNC", "0")  # force sync verify, no daemon thread
sys.path.insert(0, str(SEAWEB_ROOT / "gateway"))
import index_search  # noqa: E402 — SeaWeb/gateway/index_search.py, unmodified
import index_serve  # noqa: E402


def load_slice(limit: int | None, sample: int | None, seed: int) -> list[dict]:
    rows = [json.loads(l) for l in SLICE_PATH.open()]
    if sample:
        import random
        rows = random.Random(seed).sample(rows, min(sample, len(rows)))
    if limit:
        rows = rows[:limit]
    return rows


class LocalArtifactClient:
    """search_passages() wrapper -> list[(url, content)], MAX_DOCS-capped,
    matching seaweb_run.SeaWebClient.search()'s return shape exactly so
    seaweb_run.format_docs() (byte-identical to base_handler.
    _format_search_results_for_prompt) can be reused unmodified."""

    def __init__(self) -> None:
        health = index_serve.index_health()
        if not health.get("verified"):
            raise SystemExit(f"local artifact failed to verify: {health}")
        self.health = health

    def search(self, query: str) -> list[tuple[str, str]]:
        rows = index_search.search_passages(query, limit=MAX_DOCS)
        docs = []
        for row in rows[:MAX_DOCS]:
            url, content = row.get("url", ""), row.get("text", "")
            if url and content:
                docs.append((url, content))
        return docs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="parity", choices=["parity", "honest"])
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--random-sample", type=int, default=None)
    ap.add_argument("--seed", type=int, default=20260730)
    ap.add_argument("--claude-model", default="claude-haiku-4-5-20251001")
    ap.add_argument("--llm-workers", type=int, default=6)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows_path = out_dir / "rows.jsonl"
    done = set()
    if rows_path.exists():
        for line in rows_path.open():
            try:
                done.add(json.loads(line)["q"])
            except Exception:
                pass

    questions = load_slice(args.limit, args.random_sample, args.seed)
    todo = [r for r in questions if r["q"] not in done]
    print(f"slice=travel_place_anchored mode={args.mode} total={len(questions)} todo={len(todo)}", flush=True)

    grader = seaweb_run.grader_template()
    compose_prompt = seaweb_run.PARITY_PROMPT + (seaweb_run.HONEST_SUFFIX if args.mode == "honest" else "")
    client = LocalArtifactClient()
    print(f"local artifact health: {client.health}", flush=True)
    write_lock = threading.Lock()
    t_start = time.time()

    def compose_and_grade(item: dict, docs: list[tuple[str, str]]) -> None:
        if docs:
            predicted = seaweb_run.claude(
                compose_prompt.replace("{query}", item["q"]).replace("{docs}", seaweb_run.format_docs(docs)),
                args.claude_model,
            ) or "I cannot answer."
        else:
            predicted = "I cannot answer."
        if predicted.strip().rstrip(".").lower() == "i cannot answer":
            grade = "NOT_ATTEMPTED"
        else:
            letter = seaweb_run.claude(
                grader.replace("{question}", item["q"]).replace("{target}", item["gold"])
                .replace("{predicted}", predicted),
                args.claude_model,
            ).strip().upper()[:1]
            grade = {"A": "CORRECT", "B": "INCORRECT", "C": "NOT_ATTEMPTED"}.get(letter, "GRADE_ERROR")
        rec = {
            "q": item["q"], "gold": item["gold"], "topic": item["topic"],
            "matched_rule": item.get("matched_rule", ""), "matched_span": item.get("matched_span", ""),
            "n_docs": len(docs), "urls": [u for u, _ in docs[:3]],
            "top_doc_snippets": [{"url": u, "snippet": c[:300]} for u, c in docs[:3]],
            "predicted": predicted, "grade": grade,
        }
        with write_lock:
            with rows_path.open("a") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    with ThreadPoolExecutor(max_workers=args.llm_workers) as pool:
        futures = []
        for i, item in enumerate(todo):
            docs = client.search(item["q"])  # local sqlite — fast, no pacing needed
            futures.append(pool.submit(compose_and_grade, item, docs))
            if (i + 1) % 25 == 0:
                elapsed = time.time() - t_start
                print(f"searched {i + 1}/{len(todo)}  elapsed={elapsed:.0f}s", flush=True)
        for f in futures:
            f.result()

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
        "slice": "travel_place_anchored_20260730", "mode": args.mode, "n": n,
        "correct": c, "incorrect": i_, "not_attempted": na, "grade_error": ge,
        "attempted_rate": round(attempted / n, 4) if n else 0,
        "accuracy_given_attempted": round(acc_att, 4),
        "overall_correct": round(overall, 4),
        "f_score": round(f1, 4),
        "search": f"local frozen artifact {ARTIFACT_DIR.name} (SeaWeb/gateway/index_search.py, unmodified serving ranking), top{MAX_DOCS}",
        "composer_grader_model": args.claude_model,
        "composer_prompt": "upstream PostProcessor verbatim" + (" + abstention rule" if args.mode == "honest" else ""),
        "elapsed_s": round(time.time() - t_start, 1),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
