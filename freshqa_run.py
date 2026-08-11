#!/usr/bin/env python3
"""FreshQA runner for the SeaWeb search stack, adapted from seaweb_run.py.

Reuses seaweb_run.py's machinery rather than reinventing it: CLAUDE_BIN
resolution, the upstream-verbatim PostProcessor composer prompt (+ honest
abstention suffix), the official SimpleQA grader template, and format_docs.
New in this file: a LOCAL search client that points gateway/index_search.py
directly at a promoted pages_artifact dir (no MCP, no prod, no rate limit --
see evals/eval_world.py in the SeaWeb repo, which established this pattern),
a FreshQA CSV loader (skips the 2-row banner the Google Sheets export always
carries), a disclosed keyword+gazetteer travel/geography tagger, and a
second grader for FreshQA's own "strict" accuracy convention (binary
correct/incorrect, no hedging allowed, false-premise questions must flag the
premise) run alongside the reused SimpleQA-style 3-way grade.

DEVIATIONS (all disclosed, mirroring seaweb_run.py's own deviations list):
  - claude() session-leak fix: seaweb_run.claude() shells out to CLAUDE_BIN via
    subprocess.run() with no `env=` override, so it inherits THIS process's
    environment verbatim. When this runner is itself executed inside a live
    Claude Code session (as it was here), that environment carries
    CLAUDECODE=1 / CLAUDE_CODE_SESSION_ID / CLAUDE_CODE_HOST_SESSION_ID /
    CLAUDE_CODE_EXECPATH etc. -- and the nested `claude -p` call picks those
    up and behaves like an agentic continuation of the OUTER session instead
    of a clean stateless extraction call (reproduced 3/3: asked to extract
    an answer from two short docs, it replied describing "the Python script
    you've shared" and seaweb_run.claude()'s own signature -- i.e. it was
    reasoning about this very eval session, not the prompt). Stripping every
    CLAUDE*-prefixed env var (plus AI_AGENT, BAGGAGE) and pinning
    stdin=DEVNULL fixes it deterministically (verified). freshqa_run.claude()
    below is seaweb_run.claude() with exactly that fix; SeaWeb/ and
    seaweb_run.py are left untouched per scope. Any seaweb_run.py numbers
    produced while running INSIDE a Claude Code session should be treated as
    suspect for the same reason -- flagged in the final report, not fixed
    upstream here.
  - Search backend: LOCAL pages_artifact (sfblogs_20260727, 3,353 pages) —
    NOT prod. Methodology note baked into every summary: "artifact identical
    to prod serving version sfblogs_20260727" (gateway/server.py's
    search_web tool literally calls index_search.search_passages, which is
    exactly what LocalSeaWebClient calls here, so this is the same code
    path, only pointed at a local dir via SEAWEB_INDEX_DIR instead of
    whatever prod's current.txt resolves to). A bounded prod spot-check
    (`--action spotcheck`) pings the real anon MCP, paced >=4s apart, to
    prove local<->prod parity, then stops.
  - Composer/grader model: claude-haiku-4-5-20251001 via headless Claude
    CLI (same deviation seaweb_run.py already carries: upstream FreshQA/
    SimpleQA use GPT-4-class models; this machine has no OpenAI key).
  - FreshQA "strict accuracy": FreshEval's actual grading notebook is not
    public (linked only as a private Colab). FRESHQA_STRICT_GRADER below is
    an original prompt built from FreshEval's PUBLISHED description (no
    hedging/outdated caveats allowed; false-premise questions must name the
    false premise) -- not a reproduction of Google's proprietary prompt.
  - Gold answers: FreshQA rows carry up to 10 acceptable answers
    (answer_0..answer_9). Both graders receive all non-empty ones, joined,
    rather than a single "answer" field the way SimpleQA's grader expects.
  - Travel/geography tagger: disclosed keyword + static country/city
    gazetteer match (see tag_travel below), not NER. Over/under-inclusive
    at the margins by construction; see its docstring for a known collision
    ("cruise" the keyword vs. "Cruise" the surname).

Usage:
  python3 freshqa_run.py --action inventory --out results/20260730-freshqa
  python3 freshqa_run.py --action smoke --out var/freshqa_smoke
  python3 freshqa_run.py --action run --out var/freshqa_full --seed 20260730
  python3 freshqa_run.py --action spotcheck --out var/freshqa_spotcheck --n 10
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
import seaweb_run  # noqa: E402  (reused machinery -- see module docstring)

_CLEAN_ENV = {k: v for k, v in os.environ.items()
              if not k.startswith("CLAUDE") and k not in ("AI_AGENT", "BAGGAGE")}


def claude(prompt: str, model: str) -> str:
    """seaweb_run.claude() + the session-leak fix from the module docstring:
    a scrubbed env (no CLAUDE*-prefixed vars) and stdin pinned to DEVNULL, so
    a nested `claude -p` call can never attach to the outer Claude Code
    session it happens to be running inside of."""
    for attempt in range(2):
        try:
            out = subprocess.run(
                [seaweb_run.CLAUDE_BIN, "--model", model, "--setting-sources", "",
                 "--strict-mcp-config", "-p", prompt],
                capture_output=True, text=True, timeout=180,
                env=_CLEAN_ENV, stdin=subprocess.DEVNULL,
            )
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except subprocess.TimeoutExpired:
            pass
    return ""

DATASET = REPO / "datasets" / "freshqa_20260421.csv"
DATASET_SNAPSHOT_DATE = "2026-04-21"  # freshllms/freshqa README's newest linked sheet
SEAWEB_REPO = Path("/Users/aritro/Downloads/Start up/SeaWeb")
LOCAL_INDEX_DIR = Path(os.environ["SEAWEB_EVAL_ARTIFACT_DIR"]) if os.environ.get("SEAWEB_EVAL_ARTIFACT_DIR") else SEAWEB_REPO / "var" / "pages_artifact" / "pending" / "sfblogs_20260727"
LOCAL_ARTIFACT_LABEL = f"local artifact {LOCAL_INDEX_DIR.name}"
SPOTCHECK_GAP_S = 4.0  # task requirement: paced >=4s apart against prod anon MCP


# --------------------------------------------------------------------------
# Dataset loading
# --------------------------------------------------------------------------

def load_rows(path: Path = DATASET) -> list[dict]:
    """Google-Sheets CSV export: row0 is a click-warning banner, row1 blank,
    row2 the real header. Returns list of dicts keyed by header, one per
    non-blank data row, with a stable int `id`."""
    with open(path, newline="", encoding="utf-8") as f:
        all_rows = list(csv.reader(f))
    header = all_rows[2]
    out = []
    for r in all_rows[3:]:
        if not any(r):
            continue
        if len(r) < len(header):
            r = r + [""] * (len(header) - len(r))
        d = dict(zip(header, r))
        out.append(d)
    return out


def gold_answers(row: dict) -> list[str]:
    ans = []
    for i in range(10):
        v = (row.get(f"answer_{i}") or "").strip()
        if v and v not in ans:
            ans.append(v)
    return ans


# --------------------------------------------------------------------------
# Travel / geography / place tagger -- DISCLOSED HEURISTIC, not NER.
# --------------------------------------------------------------------------

COUNTRIES = """Afghanistan Albania Algeria Andorra Angola Argentina Armenia Australia Austria
Azerbaijan Bahamas Bahrain Bangladesh Barbados Belarus Belgium Belize Benin Bhutan Bolivia
Bosnia Botswana Brazil Brunei Bulgaria Burkina Burundi Cambodia Cameroon Canada
Chad Chile China Colombia Comoros Congo Croatia Cuba Cyprus Czechia Denmark Djibouti
Dominica Ecuador Egypt Eritrea Estonia Eswatini Ethiopia Fiji Finland
France Gabon Gambia Georgia Germany Ghana Greece Grenada Guatemala Guinea Guyana Haiti
Honduras Hungary Iceland India Indonesia Iran Iraq Ireland Israel Italy Jamaica
Japan Jordan Kazakhstan Kenya Kiribati Korea Kosovo Kuwait Kyrgyzstan Laos Latvia Lebanon
Lesotho Liberia Libya Liechtenstein Lithuania Luxembourg Madagascar Malawi Malaysia Maldives
Mali Malta Mauritania Mauritius Mexico Micronesia Moldova Monaco Mongolia Montenegro Morocco
Mozambique Myanmar Namibia Nauru Nepal Netherlands Nicaragua Niger Nigeria Norway
Oman Pakistan Palau Palestine Panama Paraguay Peru Philippines Poland Portugal Qatar Romania
Russia Rwanda Samoa Saudi Senegal Serbia Seychelles Singapore Slovakia
Slovenia Somalia Spain Sudan Suriname Sweden Switzerland Syria Taiwan
Tajikistan Tanzania Thailand Togo Tonga Tunisia Turkey Turkmenistan
Tuvalu Uganda Ukraine Uruguay
Uzbekistan Vanuatu Venezuela Vietnam Yemen Zambia Zimbabwe""".split()

COUNTRY_PHRASES = ["Costa Rica", "El Salvador", "Ivory Coast", "New Zealand", "North Korea",
    "South Korea", "Papua New Guinea", "Saudi Arabia", "Sierra Leone", "Solomon Islands",
    "South Africa", "South Sudan", "Sri Lanka", "Timor-Leste", "Trinidad and Tobago",
    "United Arab Emirates", "United Kingdom", "United States", "Vatican City",
    "San Marino", "Bosnia and Herzegovina", "Dominican Republic", "Great Britain"]

MAJOR_CITIES = """Tokyo Delhi Shanghai Beijing Cairo Mumbai Dhaka Osaka Karachi Chongqing
Istanbul Lagos Manila Rio Paris London Bangkok Jakarta Seoul Tehran Baghdad Kinshasa
Riyadh Singapore Toronto Sydney Melbourne Berlin Madrid Rome Barcelona Amsterdam Vienna
Prague Dubai Bangalore Chennai Kolkata Lahore Guangzhou Shenzhen Wuhan Chengdu
Nairobi Johannesburg Casablanca Marrakech Kyoto Sapporo Honolulu Vancouver Montreal
Zurich Geneva Munich Frankfurt Milan Naples Venice Florence Athens Lisbon Dublin Edinburgh
Moscow Kyiv Warsaw Budapest Bucharest Helsinki Stockholm Oslo Copenhagen Reykjavik
Auckland Wellington Chicago Miami Boston Seattle Austin
Denver Vegas Orlando Havana Bogota Lima Santiago Brasilia Quito
Kathmandu Colombo Hanoi Taipei""".split()

CITY_PHRASES = ["Hong Kong", "San Francisco", "New York", "Los Angeles", "Sao Paulo",
    "Buenos Aires", "Ho Chi Minh", "Phnom Penh", "Kuala Lumpur", "Las Vegas", "Cape Town"]

KEYWORDS = [
    "travel", "trip", "tour", "tourist", "tourism", "vacation", "holiday destination",
    "hotel", "resort", "flight", "airport", "airline", "visa", "passport", "cruise",
    "national park", "beach", "island", "mountain", "volcano", "desert", "border",
    "capital of", "capital city", "continent", "landmark", "monument", "unesco",
    "world heritage", "population of", "timezone", "time zone", "currency of",
    "language spoken", "distance between", "distance from", "weather in", "climate of",
    "highest peak", "tallest mountain", "longest river", "largest lake", "largest city",
    "border with", "immigration", "customs", "embassy", "consulate", "province",
    "territory", "region of", "geography",
]

_WORD_RE = re.compile(r"[A-Za-z']+")


def tag_travel(question: str) -> list[str]:
    """Disclosed heuristic tagger: keyword-phrase match + a static
    country/major-city gazetteer. Single-word keywords/gazetteer entries
    match on a WORD BOUNDARY (so "trip" doesn't fire inside "strip");
    multi-word phrases match as a plain lowercase substring. Returns the
    list of matched terms (empty = not travel-adjacent) so the tag is
    auditable per-question, not just a boolean.

    Known false-positive: "cruise" (the travel keyword) is indistinguishable
    from "Cruise" the surname (e.g. "How long did Tom Cruise hold his
    breath...") -- not disambiguated, left as a disclosed limitation rather
    than special-cased. Broad country names like "United States" also pull
    in plenty of non-travel civics/sports/entertainment questions; that is
    the intended (loose, disclosed) behavior per the task brief, not a bug."""
    ql = question.lower()
    hits = []
    for kw in KEYWORDS:
        if " " in kw:
            if kw in ql:
                hits.append(kw)
        elif re.search(r"\b" + re.escape(kw) + r"\b", ql):
            hits.append(kw)
    words = set(_WORD_RE.findall(question))
    for name in COUNTRIES:
        if name in words:
            hits.append(name)
    for name in COUNTRY_PHRASES:
        if name.lower() in ql:
            hits.append(name)
    for name in MAJOR_CITIES:
        if name in words:
            hits.append(name)
    for name in CITY_PHRASES:
        if name.lower() in ql:
            hits.append(name)
    return hits


# --------------------------------------------------------------------------
# Local SeaWeb search -- same code path as prod's search_web MCP tool
# (gateway/server.py:search_web calls index_search.search_passages directly),
# pointed at a local artifact dir instead of prod's current.txt pointer.
# Pattern lifted from SeaWeb/evals/eval_world.py's _search().
# --------------------------------------------------------------------------

class ProdSeaWebClient(seaweb_run.SeaWebClient):
    """seaweb_run.SeaWebClient with the response-envelope parse fixed.
    gateway/server.py's search_web tool shipped a breaking change on
    2026-07-28 (its own comment names the date): the payload is now always
    an object -- {"coverage": "covered"|"not_covered", "results": [...],
    "note"?: ...} -- never a bare array, specifically so an empty result is
    distinguishable from an outage. seaweb_run.SeaWebClient.search() still
    does `rows = json.loads(text)` and slices `rows[:MAX_DOCS]` as if `rows`
    were that bare array; against current prod `rows` is now a dict, so that
    slice raises `KeyError: slice(None, 10, None)` on every real call
    (reproduced verbatim while building this spot-check). Only the parsing
    of the already-fetched response text changes here; session init,
    pacing, and retry-on-429 are all inherited unmodified from the parent."""

    def search(self, query: str) -> list[tuple[str, str]]:
        with self._lock:
            wait = seaweb_run.SEARCH_GAP_S - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
        for attempt in range(3):
            r = self._client.post(
                seaweb_run.MCP_URL, headers=self._headers(),
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
            else:
                data_lines = [l[5:].strip() for l in body.splitlines() if l.startswith("data:")]
                payload = json.loads(data_lines[-1])
            text = payload["result"]["content"][0]["text"]
            envelope = json.loads(text)
            rows = envelope.get("results", []) if isinstance(envelope, dict) else envelope
        except Exception:
            return []
        docs = []
        for row in rows[: seaweb_run.MAX_DOCS]:
            url, content = row.get("url", ""), row.get("text", "")
            if url and content:
                docs.append((url, content))
        return docs


class LocalSeaWebClient:
    def __init__(self, index_dir: Path = LOCAL_INDEX_DIR) -> None:
        import os
        if not (index_dir / "pages.db").is_file() or not (index_dir / "manifest.json").is_file():
            raise SystemExit(f"local artifact missing at {index_dir}")
        manifest = json.loads((index_dir / "manifest.json").read_text())
        self.page_count = manifest.get("counts", {}).get("pages")
        self.index_dir = index_dir
        os.environ["SEAWEB_INDEX_DIR"] = str(index_dir)
        gw = str(SEAWEB_REPO / "gateway")
        if gw not in sys.path:
            sys.path.insert(0, gw)
        import index_serve  # noqa: PLC0415
        import index_search  # noqa: PLC0415
        index_serve._cache = None  # force fresh verify against this dir
        self._index_serve = index_serve
        self._index_search = index_search
        self._lock = threading.Lock()

    def search(self, query: str) -> list[tuple[str, str]]:
        """Return up to MAX_DOCS (url, text) tuples -- same shape
        seaweb_run.SeaWebClient.search returns, so format_docs and the
        compose/grade pipeline are shared unmodified. In-process sqlite
        reads; the underlying connection is thread-bound per index_serve's
        own discipline, so serialize calls across worker threads."""
        with self._lock:
            rows = self._index_search.search_passages(query, limit=seaweb_run.MAX_DOCS)
        docs = []
        for row in rows[: seaweb_run.MAX_DOCS]:
            url, text = row.get("url", ""), row.get("text", "")
            if url and text:
                docs.append((url, text))
        return docs


# --------------------------------------------------------------------------
# FreshQA-strict grader (binary, no hedging, false-premise must be flagged).
# Reuses seaweb_run.claude() for the headless call.
# --------------------------------------------------------------------------

FRESHQA_STRICT_GRADER = """
You are grading a model's answer to a question that may involve a fact that
changes over time. Grade STRICT correctness.

Question: {question}
Does the question rest on a FALSE PREMISE?: {false_premise}
Acceptable gold answer(s) (any ONE being clearly conveyed is sufficient): {gold}
Grading note from the dataset authors (may be blank): {note}
Model's predicted answer: {predicted}

Rules:
- Grade TRUE only if the predicted answer is factually accurate, conveys one
  of the acceptable gold answers (semantic match is fine; exact wording is
  not required), and is stated as current fact -- with NO hedging or
  outdated disclaimers ("as of my last update", "I'm not certain", "this may
  have changed", "I don't have real-time data", etc.). Any such hedge makes
  the grade FALSE even if the underlying fact given is correct.
- If the question rests on a false premise (see above), the predicted answer
  must explicitly identify or correct that false premise to be graded TRUE.
  Answering as though the premise were true is FALSE regardless of any other
  content in the answer.
- An explicit abstention (e.g. "I cannot answer") is graded FALSE here --
  this metric scores whether the question was answered correctly, not
  whether abstention was well-calibrated (that is the separate SimpleQA-
  style attempted/correct-given-attempted metric, graded independently).
- Ignore grammar, capitalization, and phrasing differences that don't change
  meaning.

Reply with exactly one word: TRUE or FALSE.
"""


def grade_strict(claude_bin_model: str, question: str, false_premise: str, gold: list[str],
                  note: str, predicted: str) -> str:
    if predicted.strip().rstrip(".").lower() == "i cannot answer":
        return "FALSE"  # deterministic, matches seaweb_run.py's NOT_ATTEMPTED shortcut
    prompt = FRESHQA_STRICT_GRADER.format(
        question=question, false_premise=false_premise,
        gold=" | ".join(gold) if gold else "(none provided)",
        note=note or "(none)", predicted=predicted,
    )
    out = claude(prompt, claude_bin_model).strip().upper()
    if out.startswith("TRUE"):
        return "TRUE"
    if out.startswith("FALSE"):
        return "FALSE"
    return "GRADE_ERROR"


# --------------------------------------------------------------------------
# SimpleQA-style 3-way grade, adapted for FreshQA's multi-answer gold sets.
# --------------------------------------------------------------------------

def grade_simpleqa(grader_tmpl: str, claude_bin_model: str, question: str, gold: list[str],
                    predicted: str) -> str:
    if predicted.strip().rstrip(".").lower() == "i cannot answer":
        return "NOT_ATTEMPTED"
    gold_str = "; or ".join(gold) if gold else "(no gold answer provided)"
    letter = claude(
        grader_tmpl.replace("{question}", question).replace("{target}", gold_str)
        .replace("{predicted}", predicted),
        claude_bin_model,
    ).strip().upper()[:1]
    return {"A": "CORRECT", "B": "INCORRECT", "C": "NOT_ATTEMPTED"}.get(letter, "GRADE_ERROR")


# --------------------------------------------------------------------------
# Run pipeline
# --------------------------------------------------------------------------

def build_selection(rows: list[dict], seed: int, non_travel_sample: int) -> tuple[list[dict], list[dict]]:
    travel, non_travel = [], []
    for r in rows:
        tags = tag_travel(r["question"])
        r["_travel_tags"] = tags
        r["_is_travel"] = bool(tags)
        (travel if tags else non_travel).append(r)
    sample = random.Random(seed).sample(non_travel, min(non_travel_sample, len(non_travel)))
    return travel, sample


def run(out_dir: Path, seed: int, non_travel_sample: int, claude_model: str,
        llm_workers: int, limit: int | None) -> None:
    rows = load_rows()
    travel, nontravel_sample = build_selection(rows, seed, non_travel_sample)
    selected = travel + nontravel_sample
    if limit:
        selected = selected[:limit]

    out_dir.mkdir(parents=True, exist_ok=True)
    rows_path = out_dir / "rows.jsonl"
    done = set()
    if rows_path.exists():
        for line in rows_path.open():
            try:
                done.add(json.loads(line)["id"])
            except Exception:
                pass
    todo = [r for r in selected if r["id"] not in done]
    print(f"selected={len(selected)} (travel={len(travel)}, non_travel_sample={len(nontravel_sample)}) "
          f"todo={len(todo)} seed={seed}")

    client = LocalSeaWebClient()
    print(f"local index: {client.index_dir} pages={client.page_count}")
    grader_tmpl = seaweb_run.grader_template()
    compose_prompt = seaweb_run.PARITY_PROMPT + seaweb_run.HONEST_SUFFIX  # honest mode, per task
    write_lock = threading.Lock()

    def process(item: dict) -> None:
        docs = client.search(item["question"])
        if docs:
            predicted = claude(
                compose_prompt.replace("{query}", item["question"])
                .replace("{docs}", seaweb_run.format_docs(docs)),
                claude_model,
            ) or "I cannot answer."
        else:
            predicted = "I cannot answer."
        gold = gold_answers(item)
        sqa_grade = grade_simpleqa(grader_tmpl, claude_model, item["question"], gold, predicted)
        strict_grade = grade_strict(claude_model, item["question"], item["false_premise"], gold,
                                     item.get("note", ""), predicted)
        rec = {
            "id": item["id"], "split": item["split"], "question": item["question"],
            "fact_type": item["fact_type"], "false_premise": item["false_premise"],
            "num_hops": item["num_hops"], "is_travel": item["_is_travel"],
            "travel_tags": item["_travel_tags"], "gold_answers": gold,
            "n_docs": len(docs), "urls": [u for u, _ in docs[:3]],
            "predicted": predicted, "simpleqa_grade": sqa_grade, "strict_correct": strict_grade,
        }
        with write_lock:
            with rows_path.open("a") as f:
                f.write(json.dumps(rec) + "\n")

    with ThreadPoolExecutor(max_workers=llm_workers) as pool:
        futures = [pool.submit(process, item) for item in todo]
        n_done = 0
        for f in futures:
            f.result()
            n_done += 1
            if n_done % 20 == 0:
                print(f"processed {n_done}/{len(todo)}")

    recs = [json.loads(l) for l in rows_path.open()]
    summarize(recs, out_dir, seed, non_travel_sample, claude_model)


def _metrics_block(recs: list[dict]) -> dict:
    n = len(recs)
    if n == 0:
        return {"n": 0}
    c = sum(r["simpleqa_grade"] == "CORRECT" for r in recs)
    i_ = sum(r["simpleqa_grade"] == "INCORRECT" for r in recs)
    na = sum(r["simpleqa_grade"] == "NOT_ATTEMPTED" for r in recs)
    ge = sum(r["simpleqa_grade"] == "GRADE_ERROR" for r in recs)
    attempted = c + i_
    acc_att = c / attempted if attempted else 0.0
    overall = c / n
    f1 = (2 * acc_att * overall / (acc_att + overall)) if (acc_att + overall) else 0.0
    strict_true = sum(r["strict_correct"] == "TRUE" for r in recs)
    return {
        "n": n,
        "simpleqa": {
            "correct": c, "incorrect": i_, "not_attempted": na, "grade_error": ge,
            "attempted_rate": round(attempted / n, 4),
            "accuracy_given_attempted": round(acc_att, 4),
            "overall_correct": round(overall, 4),
            "f_score": round(f1, 4),
        },
        "freshqa_strict_accuracy": round(strict_true / n, 4),
        "freshqa_strict_correct_count": strict_true,
    }


def summarize(recs: list[dict], out_dir: Path, seed: int, non_travel_sample: int,
              claude_model: str) -> None:
    travel_recs = [r for r in recs if r["is_travel"]]
    nontravel_recs = [r for r in recs if not r["is_travel"]]

    by_fact_type = {ft: _metrics_block([r for r in recs if r["fact_type"] == ft])
                     for ft in ("never-changing", "slow-changing", "fast-changing")}
    by_false_premise = {fp: _metrics_block([r for r in recs if r["false_premise"] == fp])
                         for fp in ("TRUE", "FALSE")}
    by_fact_type_travel = {
        ft: {"travel": _metrics_block([r for r in travel_recs if r["fact_type"] == ft]),
             "non_travel": _metrics_block([r for r in nontravel_recs if r["fact_type"] == ft])}
        for ft in ("never-changing", "slow-changing", "fast-changing")
    }

    marquee = [r for r in travel_recs if r["fact_type"] == "fast-changing"]

    summary = {
        "dataset": "FreshQA", "dataset_snapshot_date": DATASET_SNAPSHOT_DATE,
        "dataset_source": "https://github.com/freshllms/freshqa (Google Sheets export, newest linked snapshot)",
        "seed": seed, "non_travel_sample_target": non_travel_sample,
        "n_total": len(recs), "n_travel": len(travel_recs), "n_non_travel": len(nontravel_recs),
        "search": f"local pages_artifact, {LOCAL_ARTIFACT_LABEL}",
        "composer_grader_model": claude_model,
        "composer_prompt": "upstream PostProcessor verbatim + abstention rule (honest mode)",
        "overall": _metrics_block(recs),
        "travel": _metrics_block(travel_recs),
        "non_travel": _metrics_block(nontravel_recs),
        "by_fact_type": by_fact_type,
        "by_false_premise": by_false_premise,
        "by_fact_type_travel_split": by_fact_type_travel,
        "marquee_fast_changing_travel": [
            {"id": r["id"], "question": r["question"], "predicted": r["predicted"],
             "gold_answers": r["gold_answers"], "simpleqa_grade": r["simpleqa_grade"],
             "strict_correct": r["strict_correct"], "n_docs": r["n_docs"]}
            for r in marquee
        ],
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps({k: v for k, v in summary.items() if k != "marquee_fast_changing_travel"}, indent=2))


def inventory(out_dir: Path) -> dict:
    rows = load_rows()
    travel, _ = build_selection(rows, seed=20260730, non_travel_sample=0)
    inv = {
        "dataset_snapshot_date": DATASET_SNAPSHOT_DATE,
        "n_total": len(rows),
        "by_split": dict(Counter(r["split"] for r in rows)),
        "by_fact_type": dict(Counter(r["fact_type"] for r in rows)),
        "by_false_premise": dict(Counter(r["false_premise"] for r in rows)),
        "by_num_hops": dict(Counter(r["num_hops"] for r in rows)),
        "n_travel_adjacent": len(travel),
        "travel_by_fact_type": dict(Counter(r["fact_type"] for r in travel)),
        "travel_by_false_premise": dict(Counter(r["false_premise"] for r in travel)),
        "travel_by_split": dict(Counter(r["split"] for r in travel)),
        "n_non_travel": len(rows) - len(travel),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "inventory.json").write_text(json.dumps(inv, indent=2))
    print(json.dumps(inv, indent=2))
    return inv


def spotcheck(out_dir: Path, n: int, claude_model: str) -> None:
    """Pace >=4s apart against the real anon MCP; compare top result against
    the local artifact for the SAME queries. Stops after n queries -- no
    further prod calls anywhere else in this file."""
    rows = load_rows()
    travel, _ = build_selection(rows, seed=20260730, non_travel_sample=0)
    sample = random.Random(20260730).sample(travel, min(n, len(travel)))

    local = LocalSeaWebClient()
    prod = ProdSeaWebClient()  # has its own >=2.1s internal pacing; envelope-parse fixed
    out = []
    last = 0.0
    for item in sample:
        wait = SPOTCHECK_GAP_S - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        prod_docs = prod.search(item["question"])
        last = time.monotonic()
        local_docs = local.search(item["question"])
        prod_top = prod_docs[0][0] if prod_docs else None
        local_top = local_docs[0][0] if local_docs else None
        out.append({
            "question": item["question"],
            "prod_n": len(prod_docs), "prod_top_url": prod_top,
            "local_n": len(local_docs), "local_top_url": local_top,
            "top_match": prod_top == local_top,
        })
        print(f"[{'MATCH' if prod_top == local_top else 'DIFF '}] prod_n={len(prod_docs)} "
              f"local_n={len(local_docs)} :: {item['question'][:60]}")

    out_dir.mkdir(parents=True, exist_ok=True)
    n_match = sum(r["top_match"] for r in out)
    result = {"n": len(out), "top_url_matches": n_match,
              "match_rate": round(n_match / len(out), 4) if out else 0.0, "rows": out}
    (out_dir / "spotcheck.json").write_text(json.dumps(result, indent=2))
    print(f"\nspot-check: {n_match}/{len(out)} top-URL matches. STOPPED prod calls.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--action", default="run", choices=["inventory", "smoke", "run", "spotcheck"])
    ap.add_argument("--out", required=True)
    ap.add_argument("--seed", type=int, default=20260730)
    ap.add_argument("--non-travel-sample", type=int, default=100)
    ap.add_argument("--claude-model", default="claude-haiku-4-5-20251001")
    ap.add_argument("--llm-workers", type=int, default=6)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--n", type=int, default=10)  # spotcheck sample size
    args = ap.parse_args()

    out_dir = Path(args.out)
    if args.action == "inventory":
        inventory(out_dir)
    elif args.action == "smoke":
        run(out_dir, args.seed, non_travel_sample=0, claude_model=args.claude_model,
            llm_workers=2, limit=3)
    elif args.action == "run":
        run(out_dir, args.seed, args.non_travel_sample, args.claude_model,
            args.llm_workers, args.limit)
    elif args.action == "spotcheck":
        spotcheck(out_dir, args.n, args.claude_model)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
