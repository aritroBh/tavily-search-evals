#!/usr/bin/env python3
"""Seal-0 (SealQA hardest slice) runner for SeaWeb — the local artifact path.

Seal-0 is SealQA's 111-question adversarial slice (arXiv:2506.01062):
fact-seeking questions where web search returns conflicting, noisy or
unhelpful results, hand-picked because frontier models score near zero on
it. See datasets/README_seal0.md for exact source/revision.

Mirrors seaweb_run.py's protocol byte-for-byte and REUSES its compose/grade
machinery by import (PARITY_PROMPT, HONEST_SUFFIX, grader_template(),
claude(), format_docs(), CLAUDE_BIN) so numbers stay comparable to that
runner's SimpleQA passes. The one thing that differs is search:

  search (provider=local, default, BULK)
      In-process call to SeaWeb's OWN gateway/index_search.search_passages —
      the exact function gateway/server.py's search_web MCP tool calls
      (server.py:2075: `rows = _index_search.search_passages(query,
      limit=limit)`) — pointed at SEAWEB_INDEX_DIR=var/pages_artifact/pending/
      sfblogs_20260727 (the LOCAL pending artifact, manifest-verified below)
      instead of whatever prod's current.txt promotes. No network, no prod
      rate limit, same ranking/robots/security code prod runs.

  search (provider=prod, --limit<=5 only)
      seaweb_run.SeaWebClient over the real anonymous MCP endpoint, for a
      small spot-check that the local path's docs look like what prod would
      hand an agent. Paced >=4s/call (tighter than seaweb_run's 2.1s
      default) because other live tests share the same 20-req/60s anon
      bucket. Refuses --limit above 5 for this provider.

  compose/grade
      Byte-identical to seaweb_run.py: upstream PostProcessor extraction
      prompt (parity) or +explicit-abstention (honest), SimpleQA A/B/C
      grader template, Claude headless CLI composer+grader (same
      CLAUDE_BIN trap comment applies — bare `claude` shadows to Ollama).

Run BOTH modes over ALL 111 questions (111 x 2 = 222 graded rows, cheap):

    /path/to/SeaWeb/.venv/bin/python3 seal0_run.py --mode parity --provider local --out results/20260730-seal0/local_parity
    /path/to/SeaWeb/.venv/bin/python3 seal0_run.py --mode honest --provider local --out results/20260730-seal0/local_honest
    /path/to/SeaWeb/.venv/bin/python3 seal0_run.py --mode honest --provider prod  --limit 5 --out results/20260730-seal0/prod_spotcheck

MUST run under SeaWeb's own venv (gateway/index_search.py needs
`cryptography` for gateway/security.py, plus pyarrow for the parquet
dataset and httpx for the prod spot-check) — tavily-search-evals has none
of these installed and this script deliberately does not vendor them.

Metrics (SimpleQA standard, same formulas as seaweb_run.py): correct /
incorrect / not_attempted, attempted_rate, accuracy_given_attempted,
overall_correct, F = harmonic mean of overall_correct and
accuracy_given_attempted — PLUS this run's headline number,
confident_wrong_rate = incorrect / n, because Seal-0's whole point is that
frontier models attempt confidently and score near zero: a search layer
that instead abstains under conflicting/absent evidence should show a
confident-wrong rate near 0 even where overall_correct is low.
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
GATEWAY = SEAWEB_ROOT / "gateway"
ARTIFACT_DIR = Path(os.environ["SEAWEB_EVAL_ARTIFACT_DIR"]) if os.environ.get("SEAWEB_EVAL_ARTIFACT_DIR") else SEAWEB_ROOT / "var" / "pages_artifact" / "pending" / "sfblogs_20260727"
DATASET = REPO / "datasets" / "seal-0.parquet"

sys.path.insert(0, str(REPO))
import seaweb_run as SW  # noqa: E402  (search/compose/grade machinery, reused verbatim)

MAX_DOCS = SW.MAX_DOCS  # 10, same as seaweb_run parity
PROD_MAX_QUESTIONS = 5   # hard cap: stop-condition in the task brief
PROD_SEARCH_GAP_S = 4.0  # >=4s/call, tighter than SW.SEARCH_GAP_S (2.1s) — shared bucket


def load_seal0() -> list[dict]:
    """All 111 Seal-0 rows, verbatim from datasets/seal-0.parquet."""
    import pyarrow.parquet as pq

    if not DATASET.is_file():
        raise SystemExit(f"dataset not found: {DATASET}")
    table = pq.read_table(DATASET)
    rows = table.to_pylist()
    out = []
    for r in rows:
        out.append({
            "q": r["question"],
            "gold": r["answer"],
            "topic": r.get("topic", ""),
            "question_types": list(r.get("question_types") or []),
            "freshness": r.get("freshness", ""),
            "search_results": r.get("search_results", ""),
            "effective_year": r.get("effective_year", ""),
            "urls": list(r.get("urls") or []),
        })
    return out


def verify_local_artifact(dir: Path) -> dict:
    """Manifest-verify the LOCAL artifact the way index_serve.get_store()
    would (full-file sha256 vs manifest.json), surfaced up front so a
    corrupt/missing artifact stops the run loudly instead of silently
    degrading every search to []."""
    sys.path.insert(0, str(GATEWAY))
    import index_store  # noqa: E402

    v = index_store.verify(dir)
    return v


class LocalArtifactClient:
    """Search the LOCAL sfblogs_20260727 crawl artifact in-process, via the
    exact function prod's search_web MCP tool calls
    (gateway/server.py:2075), pointed at a local pending artifact instead of
    the promoted prod index. No network, no prod rate limit, same
    ranking/security/robots code as prod."""

    def __init__(self, artifact_dir: Path):
        result = verify_local_artifact(artifact_dir)
        if not result.get("ok"):
            raise SystemExit(f"local artifact FAILED verify(): {result}")
        self.manifest = result["manifest"]
        os.environ["SEAWEB_INDEX_DIR"] = str(artifact_dir)
        sys.path.insert(0, str(GATEWAY))
        import index_search  # noqa: E402  (module-level singleton import, same process)
        self._search_passages = index_search.search_passages

    def search(self, query: str) -> list[tuple[str, str]]:
        rows = self._search_passages(query, MAX_DOCS) or []
        docs = []
        for row in rows[:MAX_DOCS]:
            url, text = row.get("url", ""), row.get("text", "")
            if url and text:
                docs.append((url, text))
        return docs

    def search_raw(self, query: str) -> list[dict]:
        """Full rows (url/title/text/untrusted_content), for evidence capture."""
        return (self._search_passages(query, MAX_DOCS) or [])[:MAX_DOCS]


class PacedProdClient:
    """Thin wrapper around seaweb_run.SeaWebClient enforcing an EXTRA >=4s
    gap on top of its own 2.1s internal pacing, single-threaded — for the
    <=5-question prod spot-check only. Other live tests share the anon
    20-req/60s bucket, so this stays conservative rather than maximizing
    the allowance.

    Does NOT call SW.SeaWebClient.search() — that method is currently BROKEN
    against prod (discovered while building this runner, not fixed in
    seaweb_run.py per the "create NEW files only" constraint). Root cause:
    gateway/server.py's search_web tool changed its return shape on
    2026-07-28 (server.py:2095, "envelope (breaking change, 2026-07-28)")
    from a bare JSON list to `{"coverage","results","note"}`.
    SeaWebClient.search() still does `for row in rows[:MAX_DOCS]` assuming a
    list; against the new envelope `rows` is a dict, and `dict[:10]` raises
    `KeyError: slice(None, 10, None)` (reproduced live, 2026-07-30, prod
    query "Grammy Award Album of the Year record" -> that exact traceback).
    This wrapper reuses SeaWebClient's session/http-client machinery
    (self._client, self._headers(), self._sid) but parses both the legacy
    bare-list shape and the current envelope shape."""

    def __init__(self):
        self._client = SW.SeaWebClient()
        self._last = 0.0

    def search(self, query: str) -> list[tuple[str, str]]:
        wait = PROD_SEARCH_GAP_S - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        c = self._client
        for attempt in range(3):
            r = c._client.post(
                SW.MCP_URL, headers=c._headers(),
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                      "params": {"name": "search_web",
                                 "arguments": {"query": query, "limit": MAX_DOCS}}},
            )
            if r.status_code == 429:
                time.sleep(10 * (attempt + 1))
                continue
            r.raise_for_status()
            break
        self._last = time.monotonic()
        body = r.text
        try:
            if body.lstrip().startswith("{"):
                payload = json.loads(body)
            else:
                data_lines = [l[5:].strip() for l in body.splitlines() if l.startswith("data:")]
                payload = json.loads(data_lines[-1])
            text = payload["result"]["content"][0]["text"]
            parsed = json.loads(text)
        except Exception:
            return []
        rows = parsed.get("results", []) if isinstance(parsed, dict) else parsed
        docs = []
        for row in rows[:MAX_DOCS]:
            url, content = row.get("url", ""), row.get("text", "")
            if url and content:
                docs.append((url, content))
        return docs


def compose_and_grade(item: dict, docs: list[tuple[str, str]], mode: str,
                       claude_model: str, raw_docs: list[dict] | None = None) -> dict:
    grader = compose_and_grade.grader
    compose_prompt = SW.PARITY_PROMPT + (SW.HONEST_SUFFIX if mode == "honest" else "")
    if docs:
        predicted = SW.claude(
            compose_prompt.replace("{query}", item["q"]).replace("{docs}", SW.format_docs(docs)),
            claude_model,
        ) or "I cannot answer."
    else:
        predicted = "I cannot answer."  # empty result = abstention, both modes
    if predicted.strip().rstrip(".").lower() == "i cannot answer":
        grade = "NOT_ATTEMPTED"  # deterministic; no grader call needed
    else:
        letter = SW.claude(
            grader.replace("{question}", item["q"]).replace("{target}", item["gold"])
            .replace("{predicted}", predicted),
            claude_model,
        ).strip().upper()[:1]
        grade = {"A": "CORRECT", "B": "INCORRECT", "C": "NOT_ATTEMPTED"}.get(letter, "GRADE_ERROR")
    rec = {
        "q": item["q"], "gold": item["gold"], "topic": item["topic"],
        "question_types": item["question_types"], "freshness": item["freshness"],
        "search_results_label": item["search_results"], "effective_year": item["effective_year"],
        "gold_urls": item["urls"],
        "n_docs": len(docs),
        "docs": [{"url": d.get("url", ""), "title": d.get("title", ""),
                  "text": (d.get("text") or "")[:2000]} for d in (raw_docs or [])],
        "predicted": predicted, "grade": grade,
    }
    return rec


def summarize(recs: list[dict], mode: str, provider: str) -> dict:
    n = len(recs)
    c = sum(r["grade"] == "CORRECT" for r in recs)
    i_ = sum(r["grade"] == "INCORRECT" for r in recs)
    na = sum(r["grade"] == "NOT_ATTEMPTED" for r in recs)
    ge = sum(r["grade"] == "GRADE_ERROR" for r in recs)
    attempted = c + i_
    acc_att = c / attempted if attempted else 0.0
    overall = c / n if n else 0.0
    f1 = (2 * acc_att * overall / (acc_att + overall)) if (acc_att + overall) else 0.0
    return {
        "dataset": "SealQA seal_0/test (Seal-0, 111q hardest slice)",
        "provider": provider, "mode": mode, "n": n,
        "correct": c, "incorrect": i_, "not_attempted": na, "grade_error": ge,
        "attempted_rate": round(attempted / n, 4) if n else 0,
        "accuracy_given_attempted": round(acc_att, 4),
        "overall_correct": round(overall, 4),
        "f_score": round(f1, 4),
        "confident_wrong_rate": round(i_ / n, 4) if n else 0.0,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="local", choices=["local", "prod"])
    ap.add_argument("--mode", default="parity", choices=["parity", "honest"])
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--claude-model", default="claude-haiku-4-5-20251001")
    ap.add_argument("--llm-workers", type=int, default=4)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    if args.provider == "prod":
        if args.limit is None or args.limit > PROD_MAX_QUESTIONS:
            raise SystemExit(
                f"--provider prod requires --limit <= {PROD_MAX_QUESTIONS} "
                "(shared anon rate-limit bucket, other live tests running)")

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

    questions = load_seal0()
    if args.limit:
        questions = questions[: args.limit]
    todo = [q for q in questions if q["q"] not in done]
    print(f"provider={args.provider} mode={args.mode} total={len(questions)} todo={len(todo)}")

    compose_and_grade.grader = SW.grader_template()

    if args.provider == "local":
        client = LocalArtifactClient(ARTIFACT_DIR)
        print(f"local artifact VERIFIED: {ARTIFACT_DIR} "
              f"pages={client.manifest['counts']['pages']} "
              f"passages={client.manifest['counts']['passages']} "
              f"sha256={client.manifest['sha256']['pages_db'][:16]}...")
        write_lock = threading.Lock()

        def task(item: dict) -> None:
            raw = client.search_raw(item["q"])
            docs = [(r.get("url", ""), r.get("text", "")) for r in raw if r.get("url") and r.get("text")]
            rec = compose_and_grade(item, docs, args.mode, args.claude_model, raw_docs=raw)
            with write_lock:
                with rows_path.open("a") as f:
                    f.write(json.dumps(rec) + "\n")

        with ThreadPoolExecutor(max_workers=args.llm_workers) as pool:
            futures = [pool.submit(task, item) for item in todo]
            for n_done, f in enumerate(futures, 1):
                f.result()
                if n_done % 25 == 0:
                    print(f"graded {n_done}/{len(todo)}")

    else:  # prod: sequential, hard-paced, no concurrency
        client = PacedProdClient()
        for i, item in enumerate(todo):
            docs = client.search(item["q"])
            raw = [{"url": u, "title": "", "text": t} for u, t in docs]
            rec = compose_and_grade(item, docs, args.mode, args.claude_model, raw_docs=raw)
            with rows_path.open("a") as f:
                f.write(json.dumps(rec) + "\n")
            print(f"prod spot-check {i + 1}/{len(todo)}: {item['q'][:60]!r} -> {rec['grade']}")

    recs = [json.loads(l) for l in rows_path.open()]
    summary = summarize(recs, args.mode, args.provider)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
