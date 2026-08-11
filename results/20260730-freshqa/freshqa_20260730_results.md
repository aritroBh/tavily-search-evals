# FreshQA benchmark — SeaWeb honest mode — 2026-07-30

## Setup

- **Dataset**: FreshQA, snapshot **2026-04-21** (freshllms/freshqa README's newest linked Google
  Sheet as of this run; "Next update" was listed as 2026-05-11 but no newer sheet was linked —
  this is the newest the source actually publishes, not a stale pull on our end).
  Downloaded to `datasets/freshqa_20260421.csv` (600 rows: 500 TEST + 100 DEV).
- **Search backend**: LOCAL. `gateway/index_search.search_passages()` pointed at
  `SeaWeb/var/pages_artifact/pending/sfblogs_20260727` via `SEAWEB_INDEX_DIR`
  (3,353 pages / 153,171 passages per its manifest — confirms the ~3,353-page artifact named
  in the task). This is the exact function prod's `search_web` MCP tool calls
  (`gateway/server.py:2073`); only the artifact directory differs. **Methodology note: artifact
  identical to prod serving version sfblogs_20260727.**
- **Composer**: this repo's upstream-verbatim `PostProcessor` extraction prompt +
  the honest-mode abstention suffix (SeaWeb honest mode, as requested).
- **Composer/grader model**: `claude-haiku-4-5-20251001` via headless Claude CLI
  (`/Users/aritro/.local/bin/claude`, the pinned absolute path — bare `claude` in zsh on this
  machine shadows to a local Ollama proxy; verified with a 1-question smoke before any run:
  asked "what company made you," got "Anthropic").
- **Selection**: ALL travel-adjacent questions (110/600, tagged by a disclosed keyword +
  gazetteer heuristic) + a random sample of 100 non-travel questions, seed **20260730**.
  Total run: **210 questions**.
- **Grading**: two independent graders per answered question —
  1. **SimpleQA-style 3-way** (this repo's official grader template, CORRECT/INCORRECT/
     NOT_ATTEMPTED) → attempted rate, accuracy-given-attempted, overall-correct, F-score.
  2. **FreshQA-strict** (binary TRUE/FALSE, no hedging allowed, false-premise questions must
     name the premise) → "accuracy under strict grading."
  Abstentions ("I cannot answer") are graded deterministically (NOT_ATTEMPTED / FALSE) with
  no LLM call, matching this repo's existing convention.

Full run took **19.6 seconds** (well under the 2-hour budget — the local corpus is thin for
generic trivia, so most of the 210 questions abstained with zero documents and needed no LLM
call at all; only 10/210 reached the composer).

---

## Headline metrics

| Slice | N | SimpleQA attempted | SimpleQA acc·given·attempted | SimpleQA overall correct | SimpleQA F | FreshQA strict accuracy |
|---|---:|---:|---:|---:|---:|---:|
| **Overall** | 210 | 0.95% (2/210) | 100.0% (2/2) | 0.95% | 0.019 | 0.95% (2/210) |
| **Travel-adjacent** | 110 | 1.82% (2/110) | 100.0% (2/2) | 1.82% | 0.036 | 1.82% (2/110) |
| **Non-travel (sample)** | 100 | 0% (0/100) | — | 0% | 0.000 | 0% (0/100) |

Every attempted answer was correct on both graders (2/2 SimpleQA-CORRECT, 2/2 strict-TRUE).
Zero incorrect answers, zero hallucinations, across all 210 questions. The story the task set
out to test — **abstain cleanly on what you can't know, be right when you're covered** — held
100% on precision; the open question is *coverage* (see marquee section).

### By fact_type (never- / slow- / fast-changing)

| fact_type | N | SimpleQA attempted | acc·given·attempted | overall correct | F | strict accuracy |
|---|---:|---:|---:|---:|---:|---:|
| never-changing | 83 | 1.2% (1/83) | 100% | 1.2% | 0.024 | 1.2% (1/83) |
| slow-changing | 78 | 1.28% (1/78) | 100% | 1.28% | 0.025 | 1.28% (1/78) |
| fast-changing | 49 | 0% (0/49) | — | 0% | 0.000 | 0% (0/49) |

### By false_premise

| false_premise | N | SimpleQA attempted | acc·given·attempted | overall correct | F | strict accuracy |
|---|---:|---:|---:|---:|---:|---:|
| TRUE | 57 | 0% (0/57) | — | 0% | 0.000 | 0% (0/57) |
| FALSE | 153 | 1.31% (2/153) | 100% | 1.31% | 0.026 | 1.31% (2/153) |

(No false-premise question in this run's selection was ever answered — every one abstained.
That is the *safe* direction for false-premise handling — abstention on a false-premise
question can never assert the false premise as true — but it also means strict false-premise
correction was never actually exercised/verified here; see deviations.)

### fact_type × travel/non-travel

| fact_type | travel N | travel overall-correct | non-travel N | non-travel overall-correct |
|---|---:|---:|---:|---:|
| never-changing | 48 | 2.08% (1/48) | 35 | 0% |
| slow-changing | 45 | 2.22% (1/45) | 33 | 0% |
| fast-changing | 17 | 0% (0/17) | 32 | 0% |

---

## Marquee: fast-changing travel-adjacent questions (all 17, verbatim)

Every one of the 17 fast-changing + travel-adjacent questions **abstained cleanly** — SeaWeb
said "I cannot answer" every time, including the one case (id 422) where the local search
actually returned two documents (sfgate.com/49ers pages about Aldon Smith's death and Kyle
Shanahan's car crash) that did not contain the requested fact, and the composer correctly
recognized that and refused to guess. Zero fabrications, zero wrong answers — but also zero
"right + fresh" hits in this category: the sfblogs_20260727 corpus (SF-area travel/lifestyle
blogs) has essentially no live sports/weather/date rosters, so coverage on this category is a
real gap, not a hallucination risk.

| id | question | SeaWeb answer | gold | verdict |
|---|---|---|---|---|
| 378 | How many tornadoes have been confirmed by Enhanced Fujita rating in the United States so far this year? | I cannot answer. | 1,429 | ABSTAIN (correct posture, uncovered) |
| 380 | What date was the last time it rained in New York City? | I cannot answer. | Apr 17, 2026 | ABSTAIN (correct posture, uncovered) |
| 391 | On what date did the Patriots last play the Miami Dolphins? | I cannot answer. | Jan 4, 2026 | ABSTAIN (correct posture, uncovered) |
| 401 | What date is mother's day in the United States this year? | I cannot answer. | Sunday, May 10 (2026) | ABSTAIN (correct posture, uncovered) |
| 402 | What date is father's day in the United States this year? | I cannot answer. | Sunday, June 21 (2026) | ABSTAIN (correct posture, uncovered) |
| 405 | What was the highest-grossing film in the United States last year? | I cannot answer. | Zootopia 2 | ABSTAIN (correct posture, uncovered) |
| 422 | Who is the starting running back for the San Francisco 49ers? | I cannot answer. | Christian McCaffrey | ABSTAIN — 2 docs retrieved (sfgate.com/49ers, sfgate.com/sports) but neither named a current starting RB; correctly refused to guess rather than answer from the irrelevant docs or from memory |
| 449 | Who was the guest of honor at the most recent state dinner hosted by the President of the United States? | I cannot answer. | Mohammed bin Salman | ABSTAIN (correct posture, uncovered) |
| 455 | What is the latest United States jurisdiction to legalize the recreational use of cannabis? | I cannot answer. | Ohio | ABSTAIN (correct posture, uncovered) |
| 456 | What is the youngest age eligible for COVID-19 vaccination in the United States? | I cannot answer. | 6 months | ABSTAIN (correct posture, uncovered) |
| 463 | Do the Toronto Raptors have a winning record in the latest NBA season? | I cannot answer. | Yes | ABSTAIN (correct posture, uncovered) |
| 470 | Which author had the most bestselling novels in the United States last year according to Publishers Weekly? | I cannot answer. | Dav Pilkey and Rebecca Yarros | ABSTAIN (correct posture, uncovered) |
| 475 | How long has Mauna Loa been a dormant volcano since its most recent eruption? | I cannot answer. | more than 3 years | ABSTAIN (correct posture, uncovered) |
| 489 | How far did the United States representative go at the last Miss World? | I cannot answer. | Top 20 | ABSTAIN (correct posture, uncovered) |
| 579 | According to the WHO's latest reported 28-day figures, which country had the highest number of COVID-19 deaths, excluding the United States? | I cannot answer. | Sweden | ABSTAIN (correct posture, uncovered) |
| 587 | What is the name of the most recent hurricane that affected the Southeastern United States? | I cannot answer. | Imelda | ABSTAIN (correct posture, uncovered) |
| 598 | When was the most recent winner of the Tour de France born? | I cannot answer. | 1998 (Sep 21, 1998) | ABSTAIN (correct posture, uncovered) |

**The two questions SeaWeb actually answered** (both non-marquee — never/slow-changing, not
fast-changing) were both correct on both graders:

| id | fact_type | question | SeaWeb answer | gold | grade |
|---|---|---|---|---|---|
| 173 | slow-changing | Who is the President of the United States? | Trump | Donald Trump | CORRECT / TRUE |
| 556 | never-changing | What is the capital of Costa Rica? | San José | San José / San Jose | CORRECT / TRUE |

---

## Prod ↔ local parity spot-check

10 travel-adjacent queries (random, seed 20260730), paced ≥4s apart against the real anon MCP
(`api.seaweb.tech/mcp`), then **stopped** per the task's prod-call budget. Result: **10/10
top-URL matches** — but all 10 were 0-results-vs-0-results matches (both sides abstained). This
is a **weak** parity proof: with only 7/110 travel questions having any local hit at all, a
blind uniform draw of 10 from 110 had roughly a coin-flip chance of missing every hit-bearing
query entirely, and it did. It proves the *abstention path* matches, not the *retrieval-content*
path.

Two structural facts stand in for the content-path check the sample didn't exercise:
1. `LocalSeaWebClient` and prod's `search_web` tool call the exact same function
   (`index_search.search_passages`) — the only difference is which directory
   `SEAWEB_INDEX_DIR` resolves to, not the ranking/retrieval code.
2. Per this project's own operational history, prod was rolled back to and pinned on
   `sfblogs_20260727` after a regression on the following artifact — i.e., prod's live index is
   expected to be byte-identical to the artifact used here, not just code-identical.

Raw spot-check data: `spotcheck.json`. Full row-by-row local-vs-prod detail is in that file, not
reproduced here since every row was a double-empty.

---

## Methodology deviations (all disclosed)

1. **Two real bugs found and worked around, not fixed upstream** (scope forbade editing
   `SeaWeb/` or `seaweb_run.py`):
   - **Claude-CLI session leak**: `seaweb_run.claude()` shells out with no `env=` override, so
     it inherits the calling process's environment. When run *inside a live Claude Code
     session* (as this task was), that environment carries `CLAUDECODE=1` /
     `CLAUDE_CODE_SESSION_ID` / `CLAUDE_CODE_HOST_SESSION_ID` etc., and the nested `claude -p`
     call picks those up and answers as an agentic continuation of the *outer* session instead
     of a clean stateless extraction call. Reproduced 3/3: asked to extract an answer from two
     short documents, it instead described "the Python script you've shared" and
     `seaweb_run.claude()`'s own signature — i.e. it was reasoning about this very eval
     session. Fixed locally in `freshqa_run.claude()` by stripping every `CLAUDE*`-prefixed
     env var (+ `AI_AGENT`, `BAGGAGE`) and pinning `stdin=DEVNULL`; verified fixed. **Any
     seaweb_run.py numbers ever produced while running inside a Claude Code session should be
     treated as suspect for the same reason** — this wasn't specific to FreshQA.
   - **search_web envelope drift**: `gateway/server.py`'s `search_web` MCP tool shipped a
     breaking change (per its own in-code comment, dated 2026-07-28) wrapping results in
     `{"coverage": ..., "results": [...]}` instead of a bare array, specifically so an empty
     result is distinguishable from an outage. `seaweb_run.SeaWebClient.search()` still parses
     the old bare-array shape and crashes (`KeyError: slice(None, 10, None)`) on every live
     prod call today. Worked around locally with `ProdSeaWebClient` (envelope-aware parse,
     everything else inherited unmodified) for the spot-check only.
2. **FreshQA "strict accuracy"** is *not* a reproduction of Google's proprietary FreshEval
   grading notebook (only linked as a private Colab, not public source). `FRESHQA_STRICT_GRADER`
   in `freshqa_run.py` is an original prompt built from FreshEval's *published description*
   (no hedging/outdated caveats permitted; false-premise questions must name the premise).
   Treat "strict accuracy" here as an approximation of the FreshQA paper's convention, not a
   number comparable to published FreshEval results.
3. **Gold answers**: FreshQA rows carry up to 10 acceptable answers; both graders receive all
   non-empty ones joined together, rather than the single "target" string this repo's grader
   templates were built for (SimpleQA has exactly one gold answer per question).
4. **Travel/geography tagger** is a disclosed keyword + static country/city-gazetteer match,
   not NER — see `tag_travel()`'s docstring in `freshqa_run.py`. It is deliberately loose (per
   the task brief): a bare country name like "United States" tags plenty of non-travel
   civics/sports/entertainment questions as travel-adjacent. Word-boundary matching was added
   after an initial pass showed "trip" false-firing inside "strip"; one known unresolved
   homonym remains — "cruise" (the travel keyword) vs. "Cruise" (the surname, e.g. a Tom Cruise
   question) — left as a disclosed limitation rather than special-cased.
5. **Split choice**: ran on the combined TEST+DEV (600 rows) rather than TEST-only (500 rows),
   since the task asked for full-dataset travel-adjacent coverage and FreshQA distributes both
   splits in the one snapshot.
6. **Dataset snapshot staleness**: freshllms/freshqa's README lists 2026-04-21 as its newest
   sheet despite claiming weekly updates and a stated "next update: 2026-05-11" — no newer sheet
   was ever linked. This run is therefore grading against answers up to ~14 weeks old relative
   to 2026-07-30; several `fast-changing` gold answers in this dataset (e.g. "this year" dates)
   are anchored to that April snapshot's "now," not today's.
7. **Prod spot-check statistical weakness**: see the parity section above — proves the
   abstention path, not the retrieval-content path, and the ≤10-query prod budget was fully
   spent, so this was not re-run with a targeted sample.

## Files written (all new; nothing in `SeaWeb/` touched; nothing committed)

- `tavily-search-evals/datasets/freshqa_20260421.csv` — raw FreshQA snapshot (600 rows)
- `tavily-search-evals/freshqa_run.py` — the runner (inventory / smoke / run / spotcheck actions)
- `tavily-search-evals/results/20260730-freshqa/inventory.json` — full dataset inventory
- `tavily-search-evals/results/20260730-freshqa/run_summary.json` — metrics (this file's tables, machine-readable)
- `tavily-search-evals/results/20260730-freshqa/run_rows.jsonl` — all 210 per-question rows (question, docs, predicted answer, both grades)
- `tavily-search-evals/results/20260730-freshqa/spotcheck.json` — prod↔local spot-check raw rows
- `tavily-search-evals/results/20260730-freshqa/freshqa_20260730_results.md` — this file
- `tavily-search-evals/var/freshqa_smoke/`, `var/freshqa_full/`, `var/freshqa_spotcheck/` — working run directories (rows.jsonl/summary.json originals)
