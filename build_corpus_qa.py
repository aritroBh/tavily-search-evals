#!/usr/bin/env python3
"""Build a fixed-corpus travel QA set from SeaWeb's own crawled pages.

WHY THIS EXISTS. The public sets in datasets/ (SimpleQA, Seal-0, FreshQA) are
general-knowledge, and SeaWeb's corpus is travel-only, so it abstains on
almost all of them: across every completed run in results/, 3,898 of 3,915
graded questions were NOT_ATTEMPTED. That measures corpus scope, not
retrieval quality, and no re-scoring fixes it -- CRAG pays 0 for an
abstention, so a system that abstains 99.6% of the time scores ~0 however
honest it is.

What is diagnostic is BrowseComp-Plus's design (arXiv:2508.06600): fix the
corpus, derive the questions FROM it, and every miss is then unambiguously a
retrieval failure rather than a coverage gap. The answer is known to be
present, so "not found" has exactly one explanation.

The generated question is deliberately NOT the page's title. Titles are what
self_recall.py already tests, and that is the easiest possible query. These
are asked the way a person asks -- an entity plus an information need drawn
from the page's own body text -- so the set measures the gap between "the
answer is in our index" and "our engine can retrieve it".

Gold answers are verbatim spans from the crawled page, so grading needs no
external truth and cannot drift as the world changes.

Usage:
    python3 build_corpus_qa.py --index-dir ../SeaWeb/data/starter_index \
        --n 200 --out datasets/corpus_travel_qa.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SEED = 20260802

# An answerable span looks like a fact a person would ask for. These are the
# shapes that survive being cut out of a page and graded on their own.
_FACT_PATTERNS = [
    (re.compile(r"\b(open|opens|opening hours?|closed)\b[^.]{10,120}\.", re.I), "hours"),
    (re.compile(r"\b(?:costs?|price[ds]?|fee|admission|ticket)\b[^.]{10,120}\.", re.I), "price"),
    (re.compile(r"\b(?:takes?|lasts?|duration|journey time)\b[^.]{10,120}\.", re.I), "duration"),
    (re.compile(r"\b(?:located|situated|found)\s+(?:in|on|at|near)\b[^.]{10,120}\.", re.I), "location"),
    (re.compile(r"\b(?:required|must|need|visa|permit|passport)\b[^.]{10,120}\.", re.I), "requirement"),
]

_ENTITY = re.compile(r"\b([A-Z][a-zà-ÿ]{2,}(?:\s+[A-Z][a-zà-ÿ]{2,}){0,2})\b")

_TITLE_CHROME = re.compile(r"\s*[|–—·]\s*.*$")

# Page chrome and bare common nouns. An entity made only of these produces
# questions like "Where is Places located?" or "What is required to visit
# Frequently Asked Questions?" -- which grade retrieval against a subject no
# user would ever type, so they measure the generator, not the engine.
_BAD_ENTITY = frozenset(w.lower() for w in """
The This That These Those Home Index Page Pages Search Menu Contact About
Privacy Cookie Cookies Terms Read More Click Here Book Now Learn Frequently
Asked Questions Question Answer Places Place Things Thing Travel Tourism
Tourist Visit Visitor Visitors Guide Guides Info Information News Blog Post
Posts Article Articles Site Website Official Welcome Overview Discover Explore
Experience Experiences Top Best Good Great New Our Your You We Us They It
Accommodation Accommodations Hotel Hotels Restaurant Restaurants Attraction
Attractions Event Events Tour Tours Trip Trips Holiday Holidays Vacation
Destination Destinations Region Regions Area Areas City Cities Town Towns
Country Countries World Map Maps Photo Photos Gallery Video Videos Login
Register Account Cart Checkout Newsletter Subscribe Follow Share Print Email
Terms Conditions Policy Legal Imprint Sitemap Skip Content Navigation Header
Footer Main Toggle Close Open Next Previous Back Continue Submit Cancel
January February March April May June July August September October November
December Monday Tuesday Wednesday Thursday Friday Saturday Sunday
""".split())

# A question is only worth grading if its subject is a NAME. Require at least
# one token that is not ordinary English -- a real proper noun (Reykjavik,
# Nordsjaelland, Atitlan) rather than a capitalised common word.
_MIN_ENTITY_WORD = 4


def _entity_from(title: str, text: str) -> str | None:
    """The subject a person would name when asking about this page.

    Drawn from the TITLE HEAD only. A body fallback reliably grabs whatever
    proper noun a page mentions in passing, which yields a question the page
    is not actually about -- the wrong-subject defect, reproduced inside the
    benchmark that is supposed to detect it.
    """
    head = _TITLE_CHROME.sub("", title).strip()
    for cand in _ENTITY.findall(head):
        words = cand.split()
        # EVERY word must clear the chrome list: "Visit Reykjavik" is a fine
        # subject, "Visit Guide" is not, and rejecting only all-bad entities
        # would let the second through.
        if any(w.lower() in _BAD_ENTITY for w in words):
            continue
        if not any(len(w) >= _MIN_ENTITY_WORD for w in words):
            continue
        return cand
    return None


CLAUDE_BIN = "/Users/aritro/.local/bin/claude"  # never bare `claude`: zsh shadow
_CLEAN_ENV = {k: v for k, v in os.environ.items()
              if not k.startswith(("CLAUDECODE", "CLAUDE_CODE"))}

# Template questions were tried first and abandoned: pairing a title-derived
# entity with a regex-matched span produced pairs whose question and answer
# were about different things ("How long does Europe take?" against a span
# about a funicular from Interlaken). The question has to be written FROM the
# span, by something that can read it.
_GEN_PROMPT = """\
Below is a passage from a travel web page, with the page title.

TITLE: {title}
PASSAGE: {span}

Write ONE natural question a traveller would type into a search engine, whose \
answer is contained in that passage. Requirements:
- It must be answerable from the passage ALONE.
- It must name the specific place/entity, so it makes sense on its own.
- It must NOT quote the passage or copy a long phrase from it.
- If the passage is boilerplate, navigation, or has no traveller-relevant \
fact, reply exactly: SKIP

Reply with the question only, or SKIP. No preamble."""


def _gen_question(title: str, span: str, model: str) -> str | None:
    out = subprocess.run(
        [CLAUDE_BIN, "--model", model, "--setting-sources", "",
         "--strict-mcp-config", "-p",
         _GEN_PROMPT.format(title=title[:200], span=span[:600])],
        capture_output=True, text=True, timeout=180, env=_CLEAN_ENV,
    )
    if out.returncode != 0:
        return None
    q = (out.stdout or "").strip().split("\n")[0].strip()
    if not q or q.upper().startswith("SKIP") or "?" not in q:
        return None
    # A question that copied the answer grades itself.
    if len(q) > 200 or span[:40].lower() in q.lower():
        return None
    return q


def build(index_dir: Path, n: int, seed: int = SEED, *,
          model: str = "claude-haiku-4-5-20251001", workers: int = 6) -> list[dict]:
    con = sqlite3.connect(f"file:{index_dir / 'pages.db'}?mode=ro", uri=True)
    try:
        # passages.page_url is the join key, not `url` -- the passage table
        # keys by the page it was cut from.
        rows = con.execute(
            "SELECT p.url, p.title, s.text FROM pages p "
            "JOIN passages s ON s.page_url = p.url "
            "WHERE p.title IS NOT NULL AND length(s.text) > 200"
        ).fetchall()
    finally:
        con.close()

    rng = random.Random(seed)
    rng.shuffle(rows)

    out: list[dict] = []
    candidates: list[dict] = []
    seen_hosts: dict[str, int] = {}
    for url, title, text in rows:
        if len(candidates) >= n * 2:
            break
        if not title or not text:
            continue
        # Cap per host: one tourism board with 400 indexed pages would
        # otherwise dominate the set and the score would measure that host.
        host = url.split("/")[2] if "://" in url else url
        if seen_hosts.get(host, 0) >= 3:
            continue

        entity = _entity_from(title, text)
        if not entity:
            continue
        for pat, kind in _FACT_PATTERNS:
            m = pat.search(text)
            if not m:
                continue
            span = m.group(0).strip()
            if len(span) < 25:
                continue
            candidates.append({"entity": entity, "fact_kind": kind,
                               "gold_span": span, "gold_url": url,
                               "gold_title": title})
            seen_hosts[host] = seen_hosts.get(host, 0) + 1
            break

    # The question is written from the span, in parallel; a candidate whose
    # span carries no traveller-relevant fact is dropped rather than forced
    # into a template.
    def _attach(c):
        q = _gen_question(c["gold_title"], c["gold_span"], model)
        return {**c, "question": q} if q else None

    with ThreadPoolExecutor(max_workers=workers) as ex:
        built = [r for r in ex.map(_attach, candidates) if r]
    for i, r in enumerate(built[:n]):
        r["id"] = f"corpus-{i:04d}"
        out.append(r)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--index-dir", type=Path, required=True)
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--model", default="claude-haiku-4-5-20251001")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    qa = build(args.index_dir, args.n, args.seed,
               model=args.model, workers=args.workers)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as fh:
        for row in qa:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    kinds: dict[str, int] = {}
    for r in qa:
        kinds[r["fact_kind"]] = kinds.get(r["fact_kind"], 0) + 1
    print(f"wrote {len(qa)} questions -> {args.out}")
    print("by fact kind:", json.dumps(kinds))
    print("\nEvery answer is a verbatim span from a page in this index, so a "
          "miss is a RETRIEVAL failure, never a coverage gap.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
