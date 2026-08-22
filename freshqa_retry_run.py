#!/usr/bin/env python3
"""FreshQA retry-flow cassette harness — OFFLINE.

For each question: search index (skip if no artifact configured — cassette mode
uses live store only) → miss → enqueue job into a tmp jobs.db → run
fetch_lane.run_job with safe_get REPLAYED from cassette (monkeypatch-style
injection) → read_live → grade: attempted (job ran), answered (live rows
returned), confident-wrong detection = answer text present but cassette marks
the page stale/wrong (cassette record field `expect`: "answer"|"abstain").

Cassette JSONL record: {url, status, headers, body_b64, expect}
Cassette get returns FakeResult-style objects (mirror tests/test_fetch_lane.py).

Report JSON: {attempted, answered, abstained, confident_wrong, per_question:[...]}
Asserts confident_wrong == 0 (exit 1 otherwise).

Usage:
  python freshqa_retry_run.py --cassette cassettes/freshqa_smoke.jsonl --questions questions.json
  python freshqa_retry_run.py  # uses bundled 5-question smoke set
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import tempfile
import uuid
import sqlite3
import urllib.parse
from pathlib import Path
from urllib.parse import urlencode, quote, urlparse

# ---------------------------------------------------------------------------
# Bundled smoke set: 3 answerable from cassette, 2 dead-end
# ---------------------------------------------------------------------------
SMOKE_QUESTIONS = [
    {"id": "smoke_1", "question": "What is the capital of France?", "expect": "answer"},
    {"id": "smoke_2", "question": "Who wrote the novel 1984?", "expect": "answer"},
    {"id": "smoke_3", "question": "What is the largest planet in the Solar System?", "expect": "answer"},
    {"id": "smoke_4", "question": "What is the secret quantum code for project XQZ 9999?", "expect": "abstain"},
    {"id": "smoke_5", "question": "Who will win the 2035 intergalactic Olympics on Mars?", "expect": "abstain"},
]

DEFAULT_CASSETTE = Path(__file__).resolve().parent / "cassettes" / "freshqa_smoke.jsonl"

# ---------------------------------------------------------------------------
# FakeResult mirror of tests/test_fetch_lane.py
# ---------------------------------------------------------------------------
class FakeResult:
    def __init__(self, status=200, body=b"", headers=None, final_url=""):
        self.status = status
        self.body = body if isinstance(body, bytes) else body.encode("utf-8")
        self.headers = headers or {}
        self.final_url = final_url
        self.hops = []
        self.pinned_ips = []
        self.truncated = False

# ---------------------------------------------------------------------------
# Cassette loading
# ---------------------------------------------------------------------------
def load_cassette(path: Path) -> dict:
    m: dict[str, dict] = {}
    p = Path(path)
    if not p.is_file():
        return m
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        # v2 meta record: {"hostpack": {question: [candidate urls]}} feeds
        # discovery.d0_5_host_pack — the owned-discovery cutover (5073e1d1)
        # retired the wiki_search rung the v1 cassette relied on.
        if isinstance(rec.get("hostpack"), dict):
            m["__hostpack__"] = {"hostpack": rec["hostpack"]}
            continue
        url = rec.get("url")
        if not url:
            continue
        status = int(rec.get("status", 200))
        headers = rec.get("headers") or {}
        body_b64 = rec.get("body_b64") or ""
        expect = rec.get("expect") or "answer"
        try:
            body = base64.b64decode(body_b64) if body_b64 else b""
        except Exception:
            body = body_b64.encode("utf-8") if isinstance(body_b64, str) else b""
        m[url] = {"status": status, "headers": headers, "body": body, "expect": expect}
    return m

def _find_seaweb_root() -> Path | None:
    # From tavily-search-evals/freshqa_retry_run.py, SeaWeb is sibling of tavily-search-evals
    # Workspace root contains both.
    here = Path(__file__).resolve()
    # try parent.parent / SeaWeb
    cand = here.parent.parent / "SeaWeb"
    if cand.is_dir():
        return cand
    # try parents up to 4 levels
    for p in here.parents:
        cand = p / "SeaWeb"
        if cand.is_dir():
            return cand
        cand2 = p / "tavily-search-evals"
        if cand2.is_dir():
            # SeaWeb sibling of tavily-search-evals
            sea = p / "SeaWeb"
            if sea.is_dir():
                return sea
    # fallback: look relative to cwd
    cwd = Path.cwd()
    cand = cwd / "SeaWeb"
    if cand.is_dir():
        return cand
    cand = cwd.parent / "SeaWeb"
    if cand.is_dir():
        return cand
    # final: try workspace root via env
    return None

def _ensure_seaweb_on_path():
    sea = _find_seaweb_root()
    if sea is None:
        # try common location
        for cand in [Path("/Users/aritro/Downloads/Start up/SeaWeb"), Path(__file__).resolve().parents[1] / "SeaWeb", Path.cwd() / "SeaWeb", Path.cwd().parent / "SeaWeb"]:
            if cand.is_dir():
                sea = cand
                break
    if sea and str(sea) not in sys.path:
        sys.path.insert(0, str(sea))
    if sea:
        gw = sea / "gateway"
        if gw.is_dir() and str(gw) not in sys.path:
            sys.path.insert(0, str(gw))
    return sea

# Ensure SeaWeb on path at import time so subsequent imports work
_ensure_seaweb_on_path()

def _load_questions(path: Path | None) -> list[dict]:
    if path is None:
        return [dict(q) for q in SMOKE_QUESTIONS]
    p = Path(path)
    if not p.is_file():
        return [dict(q) for q in SMOKE_QUESTIONS]
    text = p.read_text(encoding="utf-8").strip()
    if not text:
        return [dict(q) for q in SMOKE_QUESTIONS]
    # try JSON array
    try:
        data = json.loads(text)
        if isinstance(data, list):
            out = []
            for item in data:
                if isinstance(item, str):
                    out.append({"id": item[:30], "question": item, "expect": "answer"})
                elif isinstance(item, dict):
                    q = item.get("question") or item.get("q") or item.get("query") or str(item)
                    expect = item.get("expect") or "answer"
                    out.append({"id": item.get("id") or q[:30], "question": q, "expect": expect, "gold": item.get("gold") or item.get("answer") or ""})
                else:
                    out.append({"id": str(item)[:30], "question": str(item), "expect": "answer"})
            if out:
                return out
        elif isinstance(data, dict) and "questions" in data:
            return _load_questions_from_list(data["questions"])
    except Exception:
        pass
    # try JSONL
    rows = []
    for line in text.splitlines():
        line=line.strip()
        if not line: continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                q = obj.get("question") or obj.get("q") or obj.get("query") or ""
                if q:
                    rows.append({"id": obj.get("id") or q[:30], "question": q, "expect": obj.get("expect") or "answer", "gold": obj.get("gold") or ""})
            elif isinstance(obj, str) and obj.strip():
                rows.append({"id": obj[:30], "question": obj, "expect": "answer"})
        except Exception:
            if line:
                rows.append({"id": line[:30], "question": line, "expect": "answer"})
    if rows:
        return rows
    # fallback to single question per line plain text
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if lines:
        return [{"id": f"q{i}", "question": l, "expect": "answer"} for i,l in enumerate(lines)]
    return [dict(q) for q in SMOKE_QUESTIONS]

def _load_questions_from_list(lst):
    out=[]
    for item in lst:
        if isinstance(item, str):
            out.append({"id": item[:30], "question": item, "expect": "answer"})
        elif isinstance(item, dict):
            q = item.get("question") or item.get("q") or ""
            out.append({"id": item.get("id") or q[:30], "question": q, "expect": item.get("expect") or "answer"})
    return out

def _try_search_index(query: str) -> list:
    """Attempt search index; skip if no artifact configured. Returns list or []."""
    idx = os.environ.get("SEAWEB_INDEX_DIR")
    if not idx:
        return []
    p = Path(idx)
    if not p.exists():
        return []
    # also need manifest etc; but we try
    try:
        _ensure_seaweb_on_path()
        # gateway path
        import importlib
        # index_search may already be loaded; ensure cache clear?
        # Use dynamic import
        spec = importlib.util.find_spec("index_search")
        if spec is None:
            # try gateway.index_search
            spec = importlib.util.find_spec("gateway.index_search")
        if spec is None:
            return []
        # import by name
        try:
            import index_search  # type: ignore
        except Exception:
            import gateway.index_search as index_search  # type: ignore
        rows = index_search.search_passages(query, limit=10)
        return rows or []
    except Exception:
        return []

def run(questions: list[dict] | None = None, cassette: str | Path | None = None, tmp_dir: str | Path | None = None, verbose: bool = False) -> dict:
    """Core retry-flow offline runner.

    Args:
        questions: list of {id, question, expect}. If None, uses SMOKE_QUESTIONS.
        cassette: path to JSONL cassette. If None, uses DEFAULT_CASSETTE.
        tmp_dir: optional base dir for live/jobs dbs. If None, creates a fresh mkdtemp.
        verbose: print per-question.
    Returns:
        summary dict with attempted, answered, abstained, confident_wrong, per_question
    """
    _ensure_seaweb_on_path()
    # imports after path ensure
    from livestore import store as live_store
    from gateway.live_read import read_live, EXPECTED_COUNSEL_HASH

    # lazy imports for patching
    import importlib
    try:
        import workers.research.fetch_lane as fetch_lane
    except Exception:
        fetch_lane = None  # type: ignore
    try:
        import workers.research.discovery as discovery
    except Exception:
        discovery = None  # type: ignore
    try:
        import workers.crawl.robots as robots_mod
    except Exception:
        robots_mod = None  # type: ignore
    try:
        import seaweb_net.safe_fetch as safe_fetch_mod
    except Exception:
        safe_fetch_mod = None  # type: ignore

    if questions is None:
        questions = [dict(q) for q in SMOKE_QUESTIONS]
    elif not questions:
        questions = [dict(q) for q in SMOKE_QUESTIONS]

    cassette_path = Path(cassette) if cassette is not None else DEFAULT_CASSETTE
    cassette_map = load_cassette(cassette_path)

    _hostpack_rec = cassette_map.pop("__hostpack__", None)
    _hostpack_raw: dict = {}
    if _hostpack_rec:
        try:
            _hostpack_raw = dict(_hostpack_rec.get("hostpack") or {})
        except Exception:
            _hostpack_raw = {}

    # Build cassette_get with flexible matching for wiki search srsearch case/encoding
    def _normalize_wiki_url(u: str) -> str | None:
        try:
            parsed = urlparse(u)
            if "w/api.php" in parsed.path and parsed.query:
                qs = urllib.parse.parse_qs(parsed.query)
                sr = qs.get("srsearch", [None])[0]
                if sr is not None:
                    # normalize srsearch lowercased for matching
                    return sr.lower().strip()
            return None
        except Exception:
            return None

    # Pre-index cassette wiki search urls by normalized srsearch
    _cassette_by_srsearch: dict[str, dict] = {}
    for _k, _v in cassette_map.items():
        n = _normalize_wiki_url(_k)
        if n is not None:
            _cassette_by_srsearch[n] = _v

    def cassette_get(url, **kw):
        rec = cassette_map.get(url)
        if rec is None:
            # Try wiki search fallback: match by normalized srsearch
            n = _normalize_wiki_url(url)
            if n is not None and n in _cassette_by_srsearch:
                rec = _cassette_by_srsearch[n]
            else:
                # Also try canonicalize url via livestore canonicalize? fallback to 404
                return FakeResult(status=404, body=b"", headers={}, final_url=url)
        return FakeResult(status=rec["status"], body=rec["body"], headers=rec["headers"], final_url=url)

    # Patch modules
    orig_fetch = None
    orig_disc = None
    orig_robots = None
    orig_safe = None
    if fetch_lane is not None:
        orig_fetch = getattr(fetch_lane, "safe_get", None)
        fetch_lane.safe_get = cassette_get  # type: ignore
    if discovery is not None:
        orig_disc = getattr(discovery, "safe_get", None)
        discovery.safe_get = cassette_get  # type: ignore
    if robots_mod is not None:
        orig_robots = getattr(robots_mod, "safe_get", None)
        robots_mod.safe_get = cassette_get  # type: ignore
    if safe_fetch_mod is not None:
        orig_safe = getattr(safe_fetch_mod, "safe_get", None)
        safe_fetch_mod.safe_get = cassette_get  # type: ignore
    orig_hostpack = None
    if discovery is not None and _hostpack_raw:
        try:
            from gateway.live_read import normalize_query as _hp_norm
        except Exception:
            def _hp_norm(s):
                return (s or "").lower().strip()
        _hostpack_map = {_hp_norm(k): list(v or []) for k, v in _hostpack_raw.items()}
        orig_hostpack = getattr(discovery, "d0_5_host_pack", None)
        discovery.d0_5_host_pack = lambda q: list(_hostpack_map.get(_hp_norm(q), []))  # type: ignore

    # Prepare tmp dirs
    base_tmp = Path(tmp_dir) if tmp_dir is not None else Path(tempfile.mkdtemp(prefix="freshqa_retry_"))
    base_tmp.mkdir(parents=True, exist_ok=True)

    # store original envs
    orig_env_live = os.environ.get("SEAWEB_LIVE_DIR")
    orig_env_crawl = os.environ.get("SEAWEB_CRAWL_MODE")
    orig_env_admit = os.environ.get("SEAWEB_ADMIT_MODE")
    orig_env_vert = os.environ.get("SEAWEB_VERTICAL_ADMIT")
    orig_env_budget = os.environ.get("SEAWEB_RESEARCH_BUDGET_MS")

    os.environ["SEAWEB_LIVE_DIR"] = str(base_tmp)
    # legal envs: ensure denylist/open/open
    os.environ["SEAWEB_CRAWL_MODE"] = "denylist"
    os.environ["SEAWEB_ADMIT_MODE"] = "open"
    os.environ["SEAWEB_VERTICAL_ADMIT"] = "open"
    # ensure budget generous
    os.environ["SEAWEB_RESEARCH_BUDGET_MS"] = os.environ.get("SEAWEB_RESEARCH_BUDGET_MS") or "20000"

    per_question = []
    attempted = 0
    answered = 0
    confident_wrong = 0

    # Reset caches before loop
    if robots_mod is not None:
        try:
            robots_mod.reset_cache()
        except Exception:
            pass
    if discovery is not None:
        try:
            discovery._robots_neg_cache.clear()
        except Exception:
            pass
        try:
            discovery._in_mem_tokens = discovery.CAPACITY  # type: ignore
            import time as _t
            discovery._in_mem_last = _t.monotonic()  # type: ignore
        except Exception:
            pass
    if fetch_lane is not None:
        try:
            fetch_lane._robots_neg_cache.clear()
        except Exception:
            pass

    for q in questions:
        q_text = str(q.get("question") or q.get("q") or q.get("query") or "").strip()
        if not q_text:
            continue
        q_id = str(q.get("id") or q_text[:40])
        q_expect = str(q.get("expect") or "answer").lower().strip()
        if q_expect not in ("answer", "abstain"):
            q_expect = "answer"

        q_tmp = base_tmp / q_id
        q_tmp.mkdir(parents=True, exist_ok=True)
        live_db = q_tmp / "live.db"
        jobs_db = q_tmp / "jobs.db"
        os.environ["SEAWEB_LIVE_DIR"] = str(q_tmp)

        if verbose:
            print(f"Q {q_id}: {q_text[:80]} expect={q_expect}", file=sys.stderr)

        # Search index skip/miss
        try:
            _ = _try_search_index(q_text)
        except Exception:
            pass
        # Always treat as miss → enqueue

        now = time.time()
        # Open writer conns
        live_conn = live_store.open_live_db(live_db, writer=True)
        jobs_conn = live_store.open_jobs_db(jobs_db, writer=True)

        # Ensure heartbeat fresh
        try:
            jobs_conn.execute(
                "INSERT INTO heartbeat(id, heartbeat_at, counsel_hash, retention_pending) VALUES (1,?,?,?) ON CONFLICT(id) DO UPDATE SET heartbeat_at=excluded.heartbeat_at, counsel_hash=excluded.counsel_hash, retention_pending=excluded.retention_pending",
                (now, EXPECTED_COUNSEL_HASH, 0),
            )
            try:
                jobs_conn.execute("COMMIT")
            except sqlite3.OperationalError:
                pass
        except Exception:
            try:
                jobs_conn.execute("ROLLBACK")
            except Exception:
                pass

        # Normalize query
        try:
            from gateway.live_read import normalize_query as _norm
            norm_q = _norm(q_text)
        except Exception:
            norm_q = q_text.lower().strip()

        job_id = None
        dedup = False
        try:
            # Use livestore enqueue
            job_id, dedup = live_store.enqueue_job(
                jobs_conn, normalized_query=norm_q, requester_key_hash="testhash", origin="search_web", depth=None, now=now
            )
        except Exception as e:
            # fallback manual
            job_id = uuid.uuid4().hex
            try:
                jobs_conn.execute(
                    "INSERT INTO jobs(job_id, normalized_query, requester_key_hash, status, origin, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                    (job_id, norm_q, "testhash", "queued", "search_web", now, now),
                )
                jobs_conn.execute(
                    "INSERT INTO job_events(job_id, ts, event, detail) VALUES (?,?,?,?)",
                    (job_id, now, "enqueued", None),
                )
                try:
                    jobs_conn.execute("COMMIT")
                except sqlite3.OperationalError:
                    pass
            except Exception:
                try:
                    jobs_conn.execute("ROLLBACK")
                except Exception:
                    pass
                job_id = None

        # Per-question reset of robots caches to ensure cassette freshness
        if robots_mod is not None:
            try:
                robots_mod.reset_cache()
            except Exception:
                pass
        if discovery is not None:
            try:
                discovery._robots_neg_cache.clear()
            except Exception:
                pass
        if fetch_lane is not None:
            try:
                fetch_lane._robots_neg_cache.clear()
            except Exception:
                pass

        attempted_here = False
        if job_id is not None:
            attempted += 1
            attempted_here = True
            # Build job_dict for fetch_lane
            try:
                row = jobs_conn.execute(
                    "SELECT job_id, normalized_query, requester_key_hash, status, origin, created_at, updated_at FROM jobs WHERE job_id=?",
                    (job_id,),
                ).fetchone()
                if row:
                    job_dict = {
                        "job_id": row[0],
                        "normalized_query": row[1],
                        "requester_key_hash": row[2],
                        "status": row[3],
                        "origin": row[4],
                        "created_at": row[5],
                        "updated_at": row[6],
                    }
                else:
                    job_dict = {"job_id": job_id, "normalized_query": norm_q, "requester_key_hash": "testhash"}
            except Exception:
                job_dict = {"job_id": job_id, "normalized_query": norm_q, "requester_key_hash": "testhash"}

            # Run fetch lane with cassette
            if fetch_lane is not None:
                try:
                    # fetch_lane.run_job will use the patched safe_get
                    _res = fetch_lane.run_job(job_dict, live_conn, jobs_conn, now=now)
                except Exception as e:
                    _res = {"status": "failed", "error": str(e)}
            else:
                _res = {"status": "failed", "error": "fetch_lane not available"}

            # Close writers before read_live (read_live opens its own)
            try:
                live_conn.close()
            except Exception:
                pass
            try:
                jobs_conn.close()
            except Exception:
                pass

            # Read live
            try:
                rows, meta = read_live(live_db, jobs_db, q_text, requester_key_hash="testhash", now=time.time())
            except Exception as e:
                rows, meta = [], {}

            is_answered = len(rows) > 0
            if is_answered:
                answered += 1
                # confident_wrong detection
                cw = False
                if q_expect == "abstain":
                    cw = True
                else:
                    # check admitted URLs vs cassette expect
                    for r in rows:
                        url = r.get("url") or r.get("canonical_url") or ""
                        rec = cassette_map.get(url)
                        if rec is None:
                            # try canonicalize
                            try:
                                from livestore.store import canonicalize_url as _canon
                                cu = _canon(url)
                                rec = cassette_map.get(cu) or cassette_map.get(url)
                            except Exception:
                                rec = None
                        if rec and rec.get("expect") == "abstain":
                            cw = True
                            break
                        # also check body contains stale marker? but expect field is primary
                    # also check per_question expect already handled
                if cw:
                    confident_wrong += 1
                per_question.append({
                    "id": q_id,
                    "question": q_text,
                    "expect": q_expect,
                    "attempted": True,
                    "answered": True,
                    "abstained": False,
                    "confident_wrong": cw,
                    "live_count": len(rows),
                    "live_meta": meta,
                })
            else:
                # abstained
                # confident_wrong should be False when abstained (no answer to be wrong)
                per_question.append({
                    "id": q_id,
                    "question": q_text,
                    "expect": q_expect,
                    "attempted": True,
                    "answered": False,
                    "abstained": True,
                    "confident_wrong": False,
                    "live_count": 0,
                    "live_meta": {},
                })
        else:
            try:
                live_conn.close()
            except Exception:
                pass
            try:
                jobs_conn.close()
            except Exception:
                pass
            per_question.append({
                "id": q_id,
                "question": q_text,
                "expect": q_expect,
                "attempted": False,
                "answered": False,
                "abstained": False,
                "confident_wrong": False,
                "live_count": 0,
                "live_meta": {},
            })

    abstained = attempted - answered
    # Restore patches
    if fetch_lane is not None and orig_fetch is not None:
        try:
            fetch_lane.safe_get = orig_fetch  # type: ignore
        except Exception:
            pass
    elif fetch_lane is not None:
        try:
            delattr(fetch_lane, "safe_get")
        except Exception:
            pass
    if discovery is not None and orig_disc is not None:
        try:
            discovery.safe_get = orig_disc  # type: ignore
        except Exception:
            pass
    if discovery is not None and orig_hostpack is not None:
        try:
            discovery.d0_5_host_pack = orig_hostpack  # type: ignore
        except Exception:
            pass
    if robots_mod is not None and orig_robots is not None:
        try:
            robots_mod.safe_get = orig_robots  # type: ignore
        except Exception:
            pass
    if safe_fetch_mod is not None and orig_safe is not None:
        try:
            safe_fetch_mod.safe_get = orig_safe  # type: ignore
        except Exception:
            pass

    # Restore env
    if orig_env_live is None:
        os.environ.pop("SEAWEB_LIVE_DIR", None)
    else:
        os.environ["SEAWEB_LIVE_DIR"] = orig_env_live
    if orig_env_crawl is None:
        os.environ.pop("SEAWEB_CRAWL_MODE", None)
    else:
        os.environ["SEAWEB_CRAWL_MODE"] = orig_env_crawl
    if orig_env_admit is None:
        os.environ.pop("SEAWEB_ADMIT_MODE", None)
    else:
        os.environ["SEAWEB_ADMIT_MODE"] = orig_env_admit
    if orig_env_vert is None:
        os.environ.pop("SEAWEB_VERTICAL_ADMIT", None)
    else:
        os.environ["SEAWEB_VERTICAL_ADMIT"] = orig_env_vert
    if orig_env_budget is None:
        os.environ.pop("SEAWEB_RESEARCH_BUDGET_MS", None)
    else:
        os.environ["SEAWEB_RESEARCH_BUDGET_MS"] = orig_env_budget

    summary = {
        "attempted": attempted,
        "answered": answered,
        "abstained": abstained,
        "confident_wrong": confident_wrong,
        "per_question": per_question,
    }
    return summary

def main() -> int:
    ap = argparse.ArgumentParser(description="FreshQA retry-flow cassette harness (offline)")
    ap.add_argument("--questions", type=Path, default=None, help="Path to questions JSON/JSONL (default: bundled 5 smoke)")
    ap.add_argument("--cassette", type=Path, default=DEFAULT_CASSETTE, help="Path to cassette JSONL")
    ap.add_argument("--out", type=Path, default=None, help="Optional output JSON path")
    ap.add_argument("--tmp-dir", type=Path, default=None, help="Optional tmp dir for live/jobs dbs")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    questions = _load_questions(args.questions)
    # cassette default may not exist if user didn't pass; fallback to bundled generation?
    cassette = args.cassette
    if cassette is None:
        cassette = DEFAULT_CASSETTE

    summary = run(questions=questions, cassette=cassette, tmp_dir=args.tmp_dir, verbose=args.verbose)

    # Print summary
    print(json.dumps(summary, indent=2))

    if args.out:
        try:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
            print(f"wrote {args.out}", file=sys.stderr)
        except Exception as e:
            print(f"failed to write out: {e}", file=sys.stderr)

    if summary.get("confident_wrong", 0) != 0:
        print(f"confident_wrong={summary['confident_wrong']} != 0 → failing", file=sys.stderr)
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
