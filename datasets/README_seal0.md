# Seal-0 (SealQA) dataset — provenance

- **Paper:** SealQA — "How Do LLMs Handle Web Search When 'Fact' Isn't
  Available?" arXiv:2506.01062 (2025)
- **HF dataset:** `vtllms/sealqa` — https://huggingface.co/datasets/vtllms/sealqa
  (canonical repo; a near-empty mirror `tuvllms/sealqa`, 13 downloads / 0
  configs found in search, was NOT used)
- **Repo commit (revision) used:** `62efe18f229ce5b1e4e2a7b056c5dd2fe53223f6`
  (`lastModified` 2026-06-23T07:21:31Z per `GET
  https://huggingface.co/api/datasets/vtllms/sealqa`)
- **Config / split:** `seal_0` / `test` — the 111-row hardest slice (the hub
  also serves `seal_hard` [254 rows] and `longseal` [254 rows]; only
  `seal_0` was requested and downloaded)
- **File fetched:** `seal-0.parquet` from
  `https://huggingface.co/datasets/vtllms/sealqa/resolve/main/seal-0.parquet`
  (HTTP 200, 26,971 bytes — matches the Hub API's reported LFS size exactly)
- **Local path:** `tavily-search-evals/datasets/seal-0.parquet`
- **sha256 (computed locally on download):**
  `0dc2812e08589690bbfe2415934dced65adffdc3c829c632ffa6c86471f9d613`
- **License:** apache-2.0
- **Row count verified:** 111 (via `pyarrow.parquet.read_table(...).num_rows`,
  matches the Hub's reported row count for `seal_0/test`)

## Schema (seal_0 config)

`question, answer, urls, freshness, question_types, effective_year,
search_results, topic, canary` — 9 columns. (`golds`/`12_docs`/`20_docs`/
`30_docs` exist only on the `longseal` config, not on `seal_0`.)

## What Seal-0 is

Per the dataset card: "SealQA is a new challenge benchmark for evaluating
SEarch-Augmented language models on fact-seeking questions where web search
yields conflicting, noisy, or unhelpful results." Seal-0 is the hardest
slice — hand-selected so that frontier models with search score near zero.
Every row here carries `search_results: "conflicting"`.
