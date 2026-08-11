#!/usr/bin/env python3
"""Re-score every completed run under CRAG's +1 / 0 / -1 accounting.

WHY THIS EXISTS. Binary grading -- the scheme every summary.json in results/
currently reports -- prices a confident wrong answer exactly like an honest
"I don't know". Both score zero. A search engine whose entire differentiator
is calibrated abstention is therefore invisible under it, which is precisely
what happened: the "honest no beats them" thesis was tested head-to-head and
lost 3-0 while never once being wrong.

OpenAI's own hallucination analysis (arXiv:2509.04664) names this as the
root cause of hallucination: binary-graded evals reward guessing, so the fix
is to penalize confident error MORE than uncertainty. The CRAG benchmark
(arXiv:2406.04744) is the standard that does it:

    CORRECT       +1
    NOT_ATTEMPTED  0     <- missing is explicitly preferred to wrong
    INCORRECT     -1

Nothing is re-run and no grade is re-judged. The per-question SimpleQA grades
already in results/**/rows.jsonl are read verbatim and re-summed under the
other accounting, so any difference between the two scoreboards is the
accounting alone. That is the whole point: the same runs, priced correctly.

Usage:
    python3 crag_score.py                      # every run under results/
    python3 crag_score.py --results-dir results/20260730-newindex-81k
    python3 crag_score.py --json-out var/crag.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

REPO = Path(__file__).resolve().parent

# Every spelling these runners have emitted for the three SimpleQA classes.
# A grade this map does not recognize is counted as ungraded and reported --
# never silently folded into NOT_ATTEMPTED, which would flatter the score by
# turning parse failures into free abstentions.
_CORRECT = {"A", "CORRECT"}
_INCORRECT = {"B", "INCORRECT"}
_ABSTAIN = {"C", "NOT_ATTEMPTED", "NOT ATTEMPTED"}

_GRADE_KEYS = ("simpleqa_grade", "grade", "correctness_grade")


def grade_of(row: dict) -> str | None:
    for k in _GRADE_KEYS:
        raw = row.get(k)
        if raw is None:
            continue
        g = str(raw).strip().upper()
        if g in _CORRECT:
            return "correct"
        if g in _INCORRECT:
            return "incorrect"
        if g in _ABSTAIN:
            return "abstain"
    return None


def score_rows(rows: list[dict]) -> dict:
    n = c = i = a = ungraded = 0
    for r in rows:
        n += 1
        g = grade_of(r)
        if g == "correct":
            c += 1
        elif g == "incorrect":
            i += 1
        elif g == "abstain":
            a += 1
        else:
            ungraded += 1

    graded = c + i + a
    attempted = c + i
    # CRAG's headline: mean score per question, in [-1, +1].
    crag = (c - i) / graded if graded else 0.0
    # Binary accuracy -- what the existing summaries report. Kept alongside so
    # the two scoreboards can be read off one line.
    binary = c / graded if graded else 0.0
    return {
        "n": n,
        "graded": graded,
        "ungraded": ungraded,
        "correct": c,
        "incorrect": i,
        "abstained": a,
        "crag_score": round(crag, 4),
        "binary_accuracy": round(binary, 4),
        "attempted_rate": round(attempted / graded, 4) if graded else 0.0,
        "accuracy_given_attempted": round(c / attempted, 4) if attempted else 0.0,
        # The number the product is actually sold on. A confident wrong answer
        # is the only failure that discredits an honest-search engine, and it
        # is the only one CRAG prices at -1.
        "confident_wrong_rate": round(i / graded, 4) if graded else 0.0,
    }


def load_rows(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results-dir", type=Path, default=REPO / "results")
    ap.add_argument("--json-out", type=Path, default=None)
    args = ap.parse_args()

    runs = {}
    for jf in sorted(args.results_dir.rglob("*.jsonl")):
        rows = load_rows(jf)
        if not rows:
            continue
        scored = score_rows(rows)
        if not scored["graded"]:
            continue
        runs[str(jf.relative_to(args.results_dir))] = scored

    print(f"{'run':52s} {'n':>5s} {'CRAG':>7s} {'binary':>7s} "
          f"{'corr':>5s} {'WRONG':>6s} {'abst':>5s}")
    print("-" * 92)
    for name, s in runs.items():
        print(f"{name[:52]:52s} {s['graded']:5d} {s['crag_score']:+7.3f} "
              f"{s['binary_accuracy']:7.3f} {s['correct']:5d} "
              f"{s['incorrect']:6d} {s['abstained']:5d}")

    if runs:
        tot_c = sum(s["correct"] for s in runs.values())
        tot_i = sum(s["incorrect"] for s in runs.values())
        tot_g = sum(s["graded"] for s in runs.values())
        print("-" * 92)
        print(f"{'ALL RUNS':52s} {tot_g:5d} {(tot_c - tot_i) / tot_g:+7.3f} "
              f"{tot_c / tot_g:7.3f} {tot_c:5d} {tot_i:6d} "
              f"{sum(s['abstained'] for s in runs.values()):5d}")
        print(f"\nconfident-wrong rate across all runs: "
              f"{tot_i / tot_g:.4f} ({tot_i}/{tot_g})")

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(runs, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
