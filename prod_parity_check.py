#!/usr/bin/env python3
"""Local <-> prod parity spot-check for the travel-anchored SimpleQA slice run.

Runs a SMALL (<=10), explicitly paced (>=4s between prod calls) set of the
slice's own queries against BOTH:
  local  - SeaWeb/gateway/index_search.search_passages() over the frozen
           sfblogs_20260727 artifact (what seaweb_run_local.py used in bulk)
  prod   - the live api.seaweb.tech anonymous MCP endpoint (search_web tool)

and diffs coverage + top-3 URLs, to empirically prove (not assert) that the
local bulk run is equivalent to what prod is serving right now.

DEVIATION FROM seaweb_run.py's SeaWebClient.search(): that class's parser
assumes the MCP tools/call result's inner JSON is a bare list[dict]
(`rows = json.loads(text); for row in rows[:MAX_DOCS]`). Probed live
2026-07-28: prod now returns an ENVELOPE, {"coverage", "results", "note"} —
a change dated the same day in gateway/server.py's own comments. Run as-is,
seaweb_run.py's parser would raise `TypeError: unhashable type: 'slice'` on
every prod call (dict sliced, not list). This script's `prod_search()`
handles both shapes so the spot-check can actually run; that mismatch is
itself part of what's being reported, not silently patched over upstream.

Usage: python3 prod_parity_check.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parent
SEAWEB_ROOT = Path("/Users/aritro/Downloads/Start up/SeaWeb")
ARTIFACT_DIR = SEAWEB_ROOT / "var" / "pages_artifact" / "pending" / "sfblogs_20260727"
SLICE_PATH = REPO / "datasets" / "travel_slice_20260730.jsonl"
MCP_URL = "https://api.seaweb.tech/mcp"
PROD_PACE_S = 4.0  # per task instruction: paced >=4s between prod calls

import os  # noqa: E402
os.environ["SEAWEB_INDEX_DIR"] = str(ARTIFACT_DIR)
os.environ.setdefault("SEAWEB_INDEX_VERIFY_ASYNC", "0")
sys.path.insert(0, str(SEAWEB_ROOT / "gateway"))
import index_search  # noqa: E402


def local_search(query: str) -> list[dict]:
    return index_search.search_passages(query, limit=10)


class ProdClient:
    def __init__(self) -> None:
        self._client = httpx.Client(timeout=45)
        r = self._client.post(
            MCP_URL,
            headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                             "clientInfo": {"name": "seaweb-parity-check", "version": "1"}}},
        )
        r.raise_for_status()
        self._sid = r.headers.get("mcp-session-id")

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self._sid:
            h["mcp-session-id"] = self._sid
        return h

    def search(self, query: str) -> dict:
        last_exc = None
        for attempt in range(3):
            try:
                r = self._client.post(
                    MCP_URL, headers=self._headers(),
                    json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                          "params": {"name": "search_web", "arguments": {"query": query}}},
                )
                r.raise_for_status()
                break
            except httpx.HTTPError as e:
                last_exc = e
                time.sleep(5 * (attempt + 1))
        else:
            raise last_exc
        body = r.text
        if body.lstrip().startswith("{"):
            payload = json.loads(body)
        else:
            data_lines = [l[5:].strip() for l in body.splitlines() if l.startswith("data:")]
            payload = json.loads(data_lines[-1])
        text = payload["result"]["content"][0]["text"]
        parsed = json.loads(text)
        # Handle BOTH shapes: the current envelope dict, and (for forward/
        # backward safety) a bare list, matching what seaweb_run.py assumed.
        if isinstance(parsed, dict):
            return {"coverage": parsed.get("coverage"), "results": parsed.get("results", [])}
        if isinstance(parsed, list):
            return {"coverage": "covered" if parsed else "not_covered", "results": parsed}
        return {"coverage": "unknown", "results": []}


def main() -> None:
    rows = [json.loads(l) for l in SLICE_PATH.open()]
    # 4 known LOCAL hits (the entire covered set from the 545-row bulk run)
    # + 6 known LOCAL misses, picked deterministically (first 6 in file
    # order) so both coverage regimes (hit and miss) get an empirical
    # local<->prod comparison, within a <=10-call prod budget.
    known_hits = [
        "Which country is the species Acanthops bidens native to?",
        "What is the Muzaffarabad Fort locally known as?",
        "In feet, what is the maximum depth of Wular Lake?",
        "What river is the Kokosing River a tributary of?",
    ]
    misses = [r["q"] for r in rows if r["q"] not in known_hits][:6]
    queries = known_hits + misses
    assert len(queries) <= 10, len(queries)

    client = ProdClient()
    results = []
    for i, q in enumerate(queries):
        local_rows = local_search(q)
        local_urls = [r.get("url", "") for r in local_rows[:3]]
        if i > 0:
            time.sleep(PROD_PACE_S)
        t0 = time.time()
        prod = client.search(q)
        prod_urls = [r.get("url", "") for r in prod["results"][:3]]
        match = (bool(local_rows) == (prod["coverage"] == "covered")) and (local_urls == prod_urls)
        rec = {
            "q": q,
            "local_n": len(local_rows), "local_top3_urls": local_urls,
            "prod_coverage": prod["coverage"], "prod_n": len(prod["results"]),
            "prod_top3_urls": prod_urls,
            "urls_match": local_urls == prod_urls,
            "coverage_match": bool(local_rows) == (prod["coverage"] == "covered"),
        }
        results.append(rec)
        print(json.dumps(rec, ensure_ascii=False))
        print(f"  [prod call latency {time.time() - t0:.2f}s]", file=sys.stderr)

    n_cov_match = sum(r["coverage_match"] for r in results)
    n_url_match = sum(r["urls_match"] for r in results)
    summary = {
        "n_queries": len(results), "prod_calls": len(results),
        "pace_s": PROD_PACE_S,
        "coverage_agreement": f"{n_cov_match}/{len(results)}",
        "top3_url_exact_agreement": f"{n_url_match}/{len(results)}",
        "rows": results,
    }
    out = REPO / "results" / "20260730-simpleqa-travel" / "prod_parity_spotcheck.json"
    out.write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    print(json.dumps({"coverage_agreement": summary["coverage_agreement"],
                       "top3_url_exact_agreement": summary["top3_url_exact_agreement"]}, indent=2))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
