#!/usr/bin/env python3
"""Score SeaWeb retrieval on the corpus-derived travel QA set.

Every question in datasets/corpus_travel_qa.jsonl was written FROM a passage
of a page in the index being searched, so the answer is known to be present.
That removes the confound that makes the public sets uninformative here: on
SimpleQA/Seal-0/FreshQA the corpus genuinely lacks most answers, so an
abstention is correct and the score measures crawl scope. Here an abstention
is always WRONG, and a miss has exactly one explanation -- retrieval.

Grading needs no model and no external truth. The gold page URL is known, so:

    hit@k        the gold page is in the top k
    empty_rate   retrieval returned nothing at all (a false absence, because
                 the answer is provably in this index)
    span_rate    the gold span's own words came back in a returned passage,
                 i.e. the caller could actually have quoted the answer

Usage:
    python3 corpus_qa_run.py --index-dir ../SeaWeb/data/starter_index
    python3 corpus_qa_run.py --index-dir ... --json-out var/corpus_qa.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
DATASET = REPO / "datasets" / "corpus_travel_qa.jsonl"

_WORD = re.compile(r"[^\W\d_]{3,}", re.UNICODE)


def _setup(index_dir: Path):
    os.environ["SEAWEB_INDEX_DIR"] = str(index_dir)
    gw = index_dir.parent.parent / "gateway"
    if not gw.exists():  # allow --index-dir anywhere under the SeaWeb repo
        gw = Path(str(index_dir).split("/data/")[0]) / "gateway"
    sys.path.insert(0, str(gw))
    import index_search
    import index_serve
    index_serve._cache = None
    return index_search.search_passages


def _span_recovered(gold_span: str, rows: list[dict]) -> bool:
    """Did the caller get back text carrying the answer's distinctive words?

    Content words only, and a majority of them, so a row that merely shares
    'the' and 'is' with the gold span does not count as having supplied it.
    """
    gold = {w.lower() for w in _WORD.findall(gold_span)}
    if not gold:
        return False
    for r in rows:
        got = {w.lower() for w in _WORD.findall(r.get("text") or "")}
        if len(gold & got) >= max(2, int(len(gold) * 0.6)):
            return True
    return False


def run(index_dir: Path, dataset: Path, limit: int = 5) -> dict:
    search = _setup(index_dir)
    rows = [json.loads(l) for l in dataset.read_text(encoding="utf-8").splitlines() if l.strip()]

    n = hit1 = hitk = empty = span = 0
    misses: list[dict] = []
    for q in rows:
        n += 1
        try:
            got = search(q["question"], limit=limit) or []
        except Exception:
            got = []
        urls = [(g.get("url") or "") for g in got]
        if not got:
            empty += 1
        if urls[:1] == [q["gold_url"]]:
            hit1 += 1
        if q["gold_url"] in urls:
            hitk += 1
        elif len(misses) < 15:
            misses.append({"q": q["question"], "gold_url": q["gold_url"],
                           "got": urls[:2], "empty": not got})
        if _span_recovered(q["gold_span"], got):
            span += 1

    d = max(n, 1)
    return {
        "n": n, "limit": limit,
        "index_dir": str(index_dir),
        "hit_at_1": round(hit1 / d, 4),
        f"hit_at_{limit}": round(hitk / d, 4),
        # Each of these is a question whose answer is provably in this index
        # and which the engine answered with nothing.
        "false_absence_rate": round(empty / d, 4),
        "span_recovered_rate": round(span / d, 4),
        "misses": misses,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--index-dir", type=Path, required=True)
    ap.add_argument("--dataset", type=Path, default=DATASET)
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--json-out", type=Path, default=None)
    args = ap.parse_args()

    res = run(args.index_dir, args.dataset, args.limit)
    print(json.dumps({k: v for k, v in res.items() if k != "misses"}, indent=2))
    print("\nMISSES (answer is in this index; retrieval did not return it):")
    for m in res["misses"][:8]:
        tag = "EMPTY" if m["empty"] else "wrong-page"
        print(f"  [{tag}] {m['q'][:88]}")
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(res, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
