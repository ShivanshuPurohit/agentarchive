#!/usr/bin/env python3
"""Evaluate the real search CLI against a private, source-checked query set."""

import argparse
import json
import pathlib
import statistics
import subprocess
import sys
import time


def evaluate(cli, cases, limit=8, repeats=1, config=None):
    results = []
    for case in cases:
        elapsed = []
        hits = []
        error = ""
        for _ in range(repeats):
            start = time.perf_counter()
            command = [sys.executable, str(cli)]
            if config is not None:
                command.extend(["--config", str(config)])
            run = subprocess.run(
                [*command, case.get("command", "search"), case["query"],
                 "--json", "--limit", str(limit)], capture_output=True, text=True,
            )
            elapsed.append(time.perf_counter() - start)
            if run.returncode:
                error = run.stderr.strip()
                break
            hits = json.loads(run.stdout)
        expected = set(case.get("expected_hit_ids", []))
        hashes = set(case.get("expected_text_sha256", []))
        ranks = [i for i, hit in enumerate(hits, 1)
                 if hit.get("hit_id") in expected or hit.get("text_sha256") in hashes or (
                     case.get("expected_session_id") and
                     hit.get("session_id") == case["expected_session_id"])]
        rank = min(ranks) if ranks else None
        passed = not error and (not hits if case.get("expect_empty") else bool(rank and rank <= 5))
        results.append({
            "id": case["id"], "query": case["query"], "seconds": statistics.median(elapsed),
            "answer_rank": rank, "passed": passed, "error": error,
            "hits": [{key: hit.get(key) for key in
                      ("hit_id", "session_id", "raw_path", "line_no", "text_sha256", "excerpt")} for hit in hits],
        })
    positive = [row for case, row in zip(cases, results) if not case.get("expect_empty")]
    times = sorted(row["seconds"] for row in results)
    return {
        "queries": len(results), "passed": sum(row["passed"] for row in results),
        "answer_at_5": sum(bool(row["answer_rank"] and row["answer_rank"] <= 5) for row in positive),
        "positive_queries": len(positive),
        "mrr_at_5": sum(1 / row["answer_rank"] for row in positive
                        if row["answer_rank"] and row["answer_rank"] <= 5) / max(1, len(positive)),
        "median_seconds": statistics.median(times), "max_seconds": max(times), "results": results,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("queries", type=pathlib.Path, help="private JSON array with checked answer hit IDs")
    parser.add_argument("--cli", type=pathlib.Path,
                        default=pathlib.Path(__file__).resolve().parents[1] / "agent_archive.py")
    parser.add_argument("--output", required=True, type=pathlib.Path)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--config", type=pathlib.Path, help="use a frozen catalog for comparable runs")
    parser.add_argument("--check", action="store_true", help="exit nonzero if a checked answer misses the top five")
    args = parser.parse_args()
    cases = json.loads(args.queries.read_text())
    if not cases or args.repeat < 1:
        parser.error("provide at least one case and a positive repeat count")
    report = evaluate(args.cli, cases, repeats=args.repeat, config=args.config)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({key: value for key, value in report.items() if key != "results"}, indent=2))
    return int(args.check and report["passed"] != report["queries"])


if __name__ == "__main__":
    raise SystemExit(main())
