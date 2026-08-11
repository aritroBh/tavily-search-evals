# Seal-0 (SealQA) benchmark — SeaWeb, 2026-07-30

## Dataset

- HF `vtllms/sealqa` @ commit `62efe18f229ce5b1e4e2a7b056c5dd2fe53223f6`, config `seal_0`, split `test`, 111 rows.
- Downloaded to `tavily-search-evals/datasets/seal-0.parquet` (sha256 `0dc2812e...9d613`, 26,971 bytes, matches Hub-reported size exactly).
- Full provenance: `tavily-search-evals/datasets/README_seal0.md`.
- Every Seal-0 row carries `search_results: "conflicting"` — this is SealQA's hand-picked hardest slice, where frontier models with search score near zero.

## Search artifact (LOCAL, not prod)

`SeaWeb/var/pages_artifact/pending/sfblogs_20260727` — manifest-verified (`index_store.verify()` returns `ok: true`; sha256 of `pages.db` matches `manifest.json` byte-for-byte): 3,353 pages / 153,171 passages, built 2026-07-27T20:48:40Z.

Runner: `tavily-search-evals/seal0_run.py` calls `gateway/index_search.search_passages()` **in-process**, pointed at this artifact via `SEAWEB_INDEX_DIR` — the exact function prod's `search_web` MCP tool calls (`gateway/server.py:2075`). No network for the bulk runs.

## Results

| Run | n | correct | incorrect | not_attempted | attempted_rate | acc\|attempted | overall_correct | F | **confident_wrong_rate** |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| local, parity mode | 111 | 0 | 0 | 111 | 0.0 | 0.0 | 0.0 | 0.0 | **0.0%** |
| local, honest mode | 111 | 0 | 0 | 111 | 0.0 | 0.0 | 0.0 | 0.0 | **0.0%** |
| prod spot-check (honest) | 5 | 0 | 0 | 5 | 0.0 | 0.0 | 0.0 | 0.0 | **0.0%** |

Composer/grader model: `claude-haiku-4-5-20251001` (headless CLI, `/Users/aritro/.local/bin/claude` — the real binary; smoke-tested first to confirm it wasn't shadowed to the Ollama proxy).

## Headline: confident-wrong rate = 0% — read the caveat before quoting this

**0 out of 111 questions, in either mode, produced an INCORRECT (confidently wrong) answer.** That is the literal number and it is reproducible. But the mechanism behind it is not "SeaWeb resolved conflicting evidence correctly" — it is **coverage abstention**:

- 110/111 questions retrieved **zero** search docs from the local artifact (and the 5 prod-spot-checked questions also retrieved zero, live). `sfblogs_20260727` is a Travel & Hospitality / San Francisco Bay Area corpus (per this repo's own crawl-scope decision), and Seal-0's questions are Entertainment/Sports/Science/Aviation/etc. general trivia — there is essentially no topical overlap.
- The 1 question that DID retrieve docs (below) got 10 results, all irrelevant homonym matches, and the composer correctly declined anyway — that's the composer's own judgment, not corpus emptiness, doing the abstaining.
- **No question in this run ever handed SeaWeb topically-relevant-but-conflicting evidence** — i.e., the actual Seal-0 stress case (evidence present AND contradictory) was never exercised. This run demonstrates the "abstain when you have nothing" reflex, not the "abstain/resolve when sources disagree" reflex the benchmark is designed to test.

So: confident-wrong rate 0% is real, but it is a coverage-gap result on this vertical-scoped corpus, not (yet) evidence that SeaWeb's conflict-resolution logic beats confidently-wrong competitors on genuinely retrieved conflicting evidence. A meaningful next step would be re-running this against a general-web-scale index (or `search_web`'s prod index on a day it's serving broader coverage) to actually exercise the conflict path.

## Marquee CORRECT answers (verbatim)

**None.** 0/111 correct in both modes — expected, given 0% attempted rate. No example to report.

## Confident-wrong cases (bug leads)

**None.** 0/111 incorrect in both modes.

## The one question with non-empty search results (full evidence)

**Q:** "What is the valency of mercury in Mercury(I) chloride?"
**Gold:** `2`
**n_docs:** 10 (only nonzero-doc question in either run)
**Predicted (both modes):** *"**No answer found in provided documents.**\n\nThe documents provided do not contain information about the valency of mercury in Mercury(I) chloride."*
**Grade:** NOT_ATTEMPTED (correct behavior — none of the 10 documents are about the chemistry of mercury)

Search results that came back, verbatim (all matched on the string "mercury" — the *planet*, the *element (as a pollutant)*, or *Freddie Mercury* — never the compound):

| # | URL | Title | Matched text (verbatim excerpt) |
|---|---|---|---|
| 1 | https://www.ngv.vic.gov.au/channel/ | Channel \| NGV National Gallery of Victoria | "Romantic landscape with Mercury and Argus / Salvator Rosa" |
| 2 | https://www.visitdubai.com/en | Visit Dubai — Official Tourism Guide | "G Save the Queen / Relive the magic of Freddy Mercury's iconic chartbusters" |
| 3 | https://www.visitdubai.com/en/real-madrid | Get ready for kick-off in Dubai \| Visit Dubai | "G Save the Queen / Relive the magic of Freddy Mercury's iconic chartbusters" |
| 4 | https://www.visitdubai.com/en/explore-dubai | Explore Dubai's Neighbourhoods, Culture & Hidden Gems | "G Save the Queen / Relive the magic of Freddy Mercury's iconic chartbusters" |
| 5 | https://www.aoml.noaa.gov/hrd/tcfaq/tcfaqHED.html | FAQ HURRICANES, TYPHOONS, AND TROPICAL CYCLONES | "...how do I convert...from inches of **mercury** to millibars..." |
| 6 | https://stagetestdomain3.nih.gov/.../niehs | National Institute of Environmental Health Sciences (NIH) | "...protecting the public from hazardous substances, such as arsenic, lead, and **mercury**." |
| 7 | https://bagong.pagasa.dost.gov.ph/astronomy | PAGASA | "...sunspot and lunar occultation observations...transits of **Mercury**, of comets and other planets." |
| 8 | https://www.pagasa.dost.gov.ph:443/astronomy | PAGASA | (duplicate of #7, different host form) |
| 9 | https://pagasa.dost.gov.ph/astronomy | PAGASA | (duplicate of #7, different host form) |
| 10 | https://www.atlasobscura.com/things-to-do/spain | 897 Cool and Unusual Things to Do in Spain — Atlas Obscura | "A beautiful but toxic fountain of **mercury**." |

This is a legitimate, correctly-abstained case (an unrelated-homonym trap that a naive "if it contains the query word" search would confidently misfire on), and it's the only piece of evidence this run generated toward the actual Seal-0 thesis.

## Bug found: `seaweb_run.py`'s prod client is currently broken

`SW.SeaWebClient.search()` (`tavily-search-evals/seaweb_run.py:149-183`) does `for row in rows[:MAX_DOCS]` assuming the parsed MCP payload is a bare list. Prod's `search_web` tool changed its return shape on 2026-07-28 (`SeaWeb/gateway/server.py` ~line 2095, its own comment says "envelope (breaking change, 2026-07-28)") to `{"coverage": ..., "results": [...], "note": ...}`. Reproduced live 2026-07-30 against `https://api.seaweb.tech/mcp`, query `"Grammy Award Album of the Year record"`:

```
KeyError: slice(None, 10, None)
  File "tavily-search-evals/seaweb_run.py", line 179, in search
    for row in rows[:MAX_DOCS]:
```

Not patched in `seaweb_run.py` (task scope: create new files only, don't modify existing ones). Worked around inside `seal0_run.py`'s `PacedProdClient`, which reimplements the HTTP call and unwraps either the legacy list shape or the current envelope. **Any other runner still using `SeaWebClient.search()` against prod unmodified is currently crashing on every single query** (the exception is not caught anywhere in that class, so it propagates to the caller rather than silently returning `[]`).

## File paths

- Runner: `tavily-search-evals/seal0_run.py`
- Dataset + provenance: `tavily-search-evals/datasets/seal-0.parquet`, `tavily-search-evals/datasets/README_seal0.md`
- Raw per-question rows: `tavily-search-evals/results/20260730-seal0/local_parity/rows.jsonl`, `.../local_honest/rows.jsonl`, `.../prod_spotcheck/rows.jsonl`
- Per-run summaries: same dirs, `summary.json`
- Combined summary: `tavily-search-evals/results/20260730-seal0/summary.json`
- This file: `tavily-search-evals/results/20260730-seal0/seal0_results.md`
