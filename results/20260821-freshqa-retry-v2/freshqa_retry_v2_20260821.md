# FreshQA retry-flow smoke — cassette v2 (hostpack), 2026-08-21

Offline cassette harness: `freshqa_retry_run.py`, bundled 5-question smoke set,
cassette `cassettes/freshqa_smoke.jsonl`.

## Why the cassette was rewritten

SeaWeb `5073e1d1 fix(live): enforce owned discovery, block Wikipedia, and fix URL
dedupe` retired the `wiki_search` discovery rung. The v1 cassette recorded only
`en.wikipedia.org` MediaWiki search + article responses, so after that cutover it
replayed responses nothing asks for any more.

v2 replaces those records with:

- one meta record `{"hostpack": {question: [candidate urls]}}`, replayed into
  `workers.research.discovery.d0_5_host_pack` (the rung `gateway/server.py:313`
  actually calls),
- `robots.txt` + page records on synthetic `*.example` hosts.

`freshqa_retry_run.py` gained the meta-record loader, the `d0_5_host_pack`
monkeypatch (query-normalized via `gateway.live_read.normalize_query`), and its
restore in the teardown block.

## Result

| Run | cassette | attempted | answered | abstained | confident_wrong | exit |
|---|---|---|---|---|---|---|
| v2 | `freshqa_smoke.jsonl` | 5 | 3 | 2 | 0 | 0 |
| control | `freshqa_smoke.jsonl.bak-20260819-wiki-v1` | 5 | 0 | 5 | 0 | 0 |

v2 matches the expected split exactly: `smoke_1/2/3` (`expect: answer`) each
returned live rows (4 / 3 / 4), `smoke_4/5` (`expect: abstain`) returned zero
rows and abstained. The harness's own assertion `confident_wrong == 0` holds.

## What the control proves

The v1 control is the falsification run, not a formality: on the same code and
the same questions it answers **0 of 3**. So the hostpack rung is load-bearing
here — v2's 3 answers come from the replayed `d0_5_host_pack` candidates, not
from some other path that would have passed either way. It also re-confirms the
cutover: the wiki rung is dead, and any harness still feeding it scores zero.

## Crossover: SeaWeb `tests/test_freshqa_cassette.py`

That SeaWeb test reads *this* cassette across the workspace
(`parents[2] / "tavily-search-evals"`), so the v2 rewrite broke
`test_cassette_contents` with `KeyError: 'url'` on the meta record — one of the
4 failures in the shared-tree full suite, and not a code regression.

`test_freshqa_cassette` (the flow test) already passed on v2: it calls
`freshqa_retry_run.run()`, which installs the hostpack patch itself, and it
asserts `answered == 3`, so it is not passing vacuously.

`test_cassette_contents` was rewritten for the v2 schema: one hostpack meta
record covering every bundled question, every candidate replayable, ≥3 HTML
pages with license meta and ≥250-byte bodies, a 200 `robots.txt` for every page
host, ≥2 dead ends that belong to abstain questions — plus a new assertion that
**no** record carries a `wikipedia.org` / `w/api.php` URL, which pins the
cutover instead of the retired rung it used to require. `_load_cassette_map`
now skips the meta record rather than keying it under `None`.

Checked that the new assertions discriminate: against the v1 backup they fail
on both new properties (0 hostpack records, 16 banned wiki URLs). Both tests
green on v2.

## Ceiling

Five questions on synthetic `.example` hosts. This proves the retry flow —
miss → enqueue → `fetch_lane.run_job` on replayed bytes → `read_live` → grade —
still works end to end after the owned-discovery cutover, and that abstention
holds on dead ends. It is **not** a recall or accuracy measurement, and the
`.example` hosts are not evidence about real publisher coverage. A parity or
quality claim needs the pre-registered N>=100 harness (workspace TODO #5).

## Reproduce

```
../SeaWeb/.venv/bin/python freshqa_retry_run.py \
  --out results/20260821-freshqa-retry-v2/run_summary.json
```

Control: add `--cassette cassettes/freshqa_smoke.jsonl.bak-20260819-wiki-v1`.

Imports SeaWeb from the shared working tree, which was dirty at run time
(branch `wip/seaweb-nextgen-handoff-20260818`, uncommitted gateway security
pass). Nothing in that diff touches `workers/research/discovery.py`, but a
clean-tree re-run is the stricter check.
