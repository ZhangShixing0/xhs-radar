#!/usr/bin/env python3
"""Build a strict, auditable detail pool from one immutable search run."""

from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


MATH_RELEVANCE = re.compile(
    r"数学|数一|数二|数三|高数|线代|概率论|极限|积分|真题|错题|复盘|题型|"
    r"660|880|1000题|张宇|武忠祥|李永乐|汤家凤"
)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]


def assign_topic(
    title: str, query_texts: set[str], config: dict[str, Any]
) -> tuple[str, str, list[str]]:
    joined_queries = " ".join(sorted(query_texts))
    scored: list[tuple[int, int, str, str, list[str]]] = []
    for rule in config["topic_rules"]:
        title_hits = [word for word in rule["keywords"] if word in title]
        query_hits = [word for word in rule["keywords"] if word in joined_queries]
        score = 4 * len(title_hits) + len(query_hits)
        scored.append(
            (
                score,
                int(rule["owned_advantage_score"]),
                str(rule["slug"]),
                str(rule["name"]),
                title_hits or query_hits,
            )
        )
    best = max(scored)
    return best[2], best[3], best[4]


def build_selection(
    run_dir: Path,
    config_path: Path,
    target: int,
    min_collected: int,
    max_per_author: int,
    max_per_topic: int,
    topic_count: int,
) -> dict[str, Any]:
    manifest = read_json(run_dir / "manifest.json")
    rows = read_jsonl(run_dir / "observations.jsonl")
    config = read_json(config_path)
    if manifest.get("status") != "completed":
        raise ValueError("Search run must be completed before detail selection")

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["feed_id"])].append(row)

    candidates: list[dict[str, Any]] = []
    distribution = {threshold: 0 for threshold in [10, 100, 500, 1000, 3000]}
    for feed_id, observations in grouped.items():
        best = max(
            observations,
            key=lambda row: (
                int(row.get("collected_count") or 0),
                int(row.get("liked_count") or 0),
            ),
        )
        collected = int(best.get("collected_count") or 0)
        for threshold in distribution:
            if collected >= threshold:
                distribution[threshold] += 1
        title = str(best.get("title") or "").strip()
        query_texts = {str(row["query_text"]) for row in observations}
        direct_math_relevance = bool(MATH_RELEVANCE.search(title))
        if collected < min_collected or not direct_math_relevance:
            continue
        topic_slug, topic_name, topic_evidence = assign_topic(
            title, query_texts, config
        )
        candidates.append(
            {
                "feed_id": feed_id,
                "canonical_url": str(best.get("canonical_url") or ""),
                "title": title,
                "author_id": str(best.get("author_id") or ""),
                "author": str(best.get("author") or ""),
                "liked_count": int(best.get("liked_count") or 0),
                "collected_count": collected,
                "comment_count": int(best.get("comment_count") or 0),
                "shared_count": int(best.get("shared_count") or 0),
                "best_result_rank": min(
                    int(row.get("result_rank") or 999) for row in observations
                ),
                "query_count": len(query_texts),
                "source_queries": sorted(query_texts),
                "topic_slug": topic_slug,
                "topic_name": topic_name,
                "topic_evidence": topic_evidence,
                "qualification_tier": "proven_save_1000",
                "qualification_reason": (
                    f"collected>={min_collected}; direct_math_title; "
                    "topic_cross_author_required"
                ),
            }
        )

    by_topic: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        by_topic[candidate["topic_slug"]].append(candidate)
    topic_stats: list[dict[str, Any]] = []
    for slug, items in by_topic.items():
        authors = {item["author_id"] or item["author"] for item in items}
        topic_stats.append(
            {
                "slug": slug,
                "name": items[0]["topic_name"],
                "candidate_count": len(items),
                "distinct_author_count": len(authors),
                "median_collected": statistics.median(
                    item["collected_count"] for item in items
                ),
            }
        )
    qualifying_topics = [
        item for item in topic_stats if item["distinct_author_count"] >= 3
    ]
    qualifying_topics.sort(
        key=lambda item: (
            item["distinct_author_count"],
            item["median_collected"],
            item["candidate_count"],
        ),
        reverse=True,
    )
    selected_topic_slugs = {
        item["slug"] for item in qualifying_topics[:topic_count]
    }

    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    author_counts: dict[str, int] = defaultdict(int)
    topic_counts: dict[str, int] = defaultdict(int)
    ranked = sorted(
        [item for item in candidates if item["topic_slug"] in selected_topic_slugs],
        key=lambda item: (
            item["collected_count"],
            item["liked_count"],
            -item["best_result_rank"],
        ),
        reverse=True,
    )
    while len(selected) < target:
        added = False
        for topic in qualifying_topics[:topic_count]:
            slug = topic["slug"]
            if topic_counts[slug] >= max_per_topic:
                continue
            for candidate in ranked:
                if candidate["topic_slug"] != slug:
                    continue
                if candidate["feed_id"] in selected_ids:
                    continue
                author_key = candidate["author_id"] or candidate["author"]
                if author_counts[author_key] >= max_per_author:
                    continue
                candidate = dict(candidate)
                candidate["selection_rank"] = len(selected) + 1
                candidate["selection_reason"] = "topic_balanced_high_save"
                selected.append(candidate)
                selected_ids.add(candidate["feed_id"])
                author_counts[author_key] += 1
                topic_counts[slug] += 1
                added = True
                break
            if len(selected) >= target:
                break
        if not added:
            break

    if len(selected) < target:
        for candidate in ranked:
            if len(selected) >= target:
                break
            if candidate["feed_id"] in selected_ids:
                continue
            author_key = candidate["author_id"] or candidate["author"]
            if author_counts[author_key] >= max_per_author:
                continue
            if topic_counts[candidate["topic_slug"]] >= max_per_topic:
                continue
            candidate = dict(candidate)
            candidate["selection_rank"] = len(selected) + 1
            candidate["selection_reason"] = "global_fill_high_save"
            selected.append(candidate)
            selected_ids.add(candidate["feed_id"])
            author_counts[author_key] += 1
            topic_counts[candidate["topic_slug"]] += 1

    if any(item["collected_count"] < min_collected for item in selected):
        raise AssertionError("Selected detail candidate below hard collection gate")
    if len({item["feed_id"] for item in selected}) != len(selected):
        raise AssertionError("Duplicate feed identity in detail selection")
    if max(author_counts.values(), default=0) > max_per_author:
        raise AssertionError("Author concentration exceeds configured limit")

    return {
        "schema_version": "2.0",
        "collector_run_id": str(manifest["run_id"]),
        "gate": {
            "min_collected": min_collected,
            "direct_math_title_required": True,
            "min_topic_authors": 3,
            "max_per_author": max_per_author,
            "max_per_topic": max_per_topic,
            "target": target,
            "growth_exception": "disabled_until_24h_snapshot_exists",
        },
        "distribution": {
            "unique_notes": len(grouped),
            **{f"ge_{key}": value for key, value in distribution.items()},
        },
        "eligible_count": len(candidates),
        "qualifying_topics": qualifying_topics,
        "selected_count": len(selected),
        "selected": selected,
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--target", type=int, default=20)
    parser.add_argument("--min-collected", type=int, default=1000)
    parser.add_argument("--max-per-author", type=int, default=1)
    parser.add_argument("--max-per-topic", type=int, default=7)
    parser.add_argument("--topic-count", type=int, default=5)
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    result = build_selection(
        run_dir,
        args.config.resolve(),
        args.target,
        args.min_collected,
        args.max_per_author,
        args.max_per_topic,
        args.topic_count,
    )
    json_path = run_dir / "detail-selection-v2.json"
    csv_path = run_dir / "detail-selection-v2.csv"
    json_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8-sig",
    )
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        fieldnames = [
            "selection_rank",
            "feed_id",
            "title",
            "author",
            "collected_count",
            "liked_count",
            "comment_count",
            "topic_name",
            "best_result_rank",
            "query_count",
            "source_queries",
            "qualification_tier",
            "selection_reason",
            "canonical_url",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for item in result["selected"]:
            row = {key: item.get(key, "") for key in fieldnames}
            row["source_queries"] = "|".join(item["source_queries"])
            writer.writerow(row)
    print(
        json.dumps(
            {
                "status": "completed",
                "eligible": result["eligible_count"],
                "selected": result["selected_count"],
                "json": str(json_path),
                "csv": str(csv_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
