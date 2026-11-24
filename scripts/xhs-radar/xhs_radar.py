#!/usr/bin/env python3
"""Auditable local radar for Xiaohongshu postgraduate-math research.

The program intentionally uses only Python's standard library. It never calls
the platform. Collection remains the responsibility of the existing read-only
PowerShell collectors; this program persists, deduplicates and analyses their
outputs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from tempfile import NamedTemporaryFile
import math
import re
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# Persisted identifier used when measuring overlap with the device's first import.
BASELINE_RUN_ID = "baseline-2026-07-31"


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def iso_from_unix_ms(value: Any) -> str | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return datetime.fromtimestamp(number / 1000, tz=timezone.utc).isoformat()


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def initialize(db_path: Path, schema_path: Path) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(schema_path.read_text(encoding="utf-8-sig"))
        conn.commit()
    finally:
        conn.close()


def upsert_queries_and_topics(
    conn: sqlite3.Connection, config: dict[str, Any], stamp: str
) -> None:
    for query in config["queries"]:
        conn.execute(
            """
            INSERT INTO search_queries(
                query_key, query_text, group_name, active, priority,
                source_note, created_at, updated_at
            ) VALUES (?, ?, ?, 1, ?, ?, ?, ?)
            ON CONFLICT(query_key) DO UPDATE SET
                query_text=excluded.query_text,
                group_name=excluded.group_name,
                active=excluded.active,
                priority=excluded.priority,
                source_note=excluded.source_note,
                updated_at=excluded.updated_at
            """,
            (
                query["key"],
                query["text"],
                query["group"],
                int(query.get("priority", 100)),
                query.get("source"),
                stamp,
                stamp,
            ),
        )
    for topic in config["topic_rules"]:
        conn.execute(
            """
            INSERT INTO topics(
                slug, name, description, owned_advantage_score,
                owned_advantage_evidence, active, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(slug) DO UPDATE SET
                name=excluded.name,
                description=excluded.description,
                owned_advantage_score=excluded.owned_advantage_score,
                owned_advantage_evidence=excluded.owned_advantage_evidence,
                active=1,
                updated_at=excluded.updated_at
            """,
            (
                topic["slug"],
                topic["name"],
                "；".join(topic["keywords"]),
                float(topic["owned_advantage_score"]),
                topic["owned_evidence"],
                stamp,
                stamp,
            ),
        )


def classify_note(
    sample: dict[str, Any],
    detail: dict[str, Any] | None,
    config: dict[str, Any],
) -> list[tuple[str, float, str]]:
    title = str((detail or {}).get("title") or sample.get("title") or "")
    description = str((detail or {}).get("description") or "")
    source_keywords = " ".join(str(x) for x in sample.get("source_keywords", []))
    title_and_description = title + " " + description
    combined = title_and_description + " " + source_keywords
    matches: list[tuple[str, float, str]] = []
    for rule in config["topic_rules"]:
        title_hits = [word for word in rule["keywords"] if word in title_and_description]
        source_hits = [word for word in rule["keywords"] if word in source_keywords]
        if title_hits:
            confidence = min(0.92, 0.72 + 0.05 * len(title_hits))
            matches.append(
                (rule["slug"], confidence, "正文/标题命中:" + "|".join(title_hits))
            )
        elif source_hits and any(word in combined for word in source_hits):
            matches.append(
                (rule["slug"], 0.58, "搜索词命中:" + "|".join(source_hits))
            )
    matches.sort(key=lambda item: (-item[1], item[0]))
    return matches[:3]


def import_collector_run(
    db_path: Path,
    schema_path: Path,
    config_path: Path,
    run_dir: Path,
) -> dict[str, Any]:
    """Import one immutable collector run as one database run per radar layer."""
    initialize(db_path, schema_path)
    manifest = read_json(run_dir / "manifest.json")
    rows = read_jsonl(run_dir / "observations.jsonl")
    if manifest.get("status") != "completed":
        raise ValueError("Collector run must be completed before import")
    if int(manifest["counts"]["operations_passed"]) != int(
        manifest["counts"]["operations_selected"]
    ):
        raise ValueError("Collector manifest is not a complete operation set")
    if len(rows) != int(manifest["counts"]["observations"]):
        raise ValueError("Observation count does not match collector manifest")
    if any(str(row.get("run_id")) != str(manifest["run_id"]) for row in rows):
        raise ValueError("Observation run identity does not match manifest")

    config = read_json(config_path)
    plan_path = Path(str(manifest["plan_path"]))
    plan = read_json(plan_path)
    stamp = now_iso()
    collector_run_id = str(manifest["run_id"])
    layers = sorted({str(row["layer"]) for row in rows})
    unsupported = set(layers) - {"recent", "topic", "classic"}
    if unsupported:
        raise ValueError(f"Unsupported collector layers: {sorted(unsupported)}")
    database_run_ids = {
        layer: f"{collector_run_id}-{layer}" for layer in layers
    }
    conn = connect(db_path)
    try:
        before = table_counts(conn)
        with conn:
            upsert_queries_and_topics(conn, config, stamp)
            # Collector plans may be project-specific and therefore absent from the
            # central postgraduate-math keyword config. Register their immutable
            # query definitions before importing observations so every read-only
            # market-research run can still enter the shared deduplication store.
            allowed_groups = {"fixed", "seasonal", "decision", "exploration", "legacy"}
            for operation in plan.get("operations", []):
                query_key = str(operation["query_key"])
                query_text = str(operation["query_text"])
                group_name = str(operation.get("query_group") or "exploration")
                if group_name not in allowed_groups:
                    group_name = "exploration"
                source_note = str(
                    operation.get("source")
                    or plan.get("round")
                    or "collector_plan"
                )
                conn.execute(
                    """
                    INSERT INTO search_queries(
                        query_key, query_text, group_name, active, priority,
                        source_note, created_at, updated_at
                    ) VALUES (?, ?, ?, 1, 100, ?, ?, ?)
                    ON CONFLICT(query_key) DO NOTHING
                    """,
                    (query_key, query_text, group_name, source_note, stamp, stamp),
                )
            for layer, database_run_id in database_run_ids.items():
                conn.execute(
                    """
                    INSERT INTO radar_runs(
                        id, layer, status, started_at, completed_at,
                        config_hash, source_identity, notes
                    ) VALUES (?, ?, 'completed', ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        status='completed', completed_at=excluded.completed_at,
                        config_hash=excluded.config_hash,
                        source_identity=excluded.source_identity,
                        notes=excluded.notes
                    """,
                    (
                        database_run_id,
                        layer,
                        str(manifest["started_at"]),
                        str(manifest["completed_at"]),
                        str(manifest["plan_sha256"]),
                        str(Path("data", "xhs-radar", "runs", run_dir.name)).replace("\\", "/"),
                        "Collector run split by layer; planned and applied filters are auditable in source JSONL.",
                    ),
                )

            query_ids = {
                str(row["query_key"]): int(row["id"])
                for row in conn.execute("SELECT id, query_key FROM search_queries")
            }
            missing_query_keys = sorted(
                {str(row["query_key"]) for row in rows} - set(query_ids)
            )
            if missing_query_keys:
                raise ValueError(
                    "Collector plan queries were not registered: "
                    + ", ".join(missing_query_keys)
                )
            topic_ids = {
                str(row["slug"]): int(row["id"])
                for row in conn.execute("SELECT id, slug FROM topics")
            }
            latest_snapshot: dict[tuple[str, int], dict[str, Any]] = {}
            note_sources: dict[str, set[str]] = defaultdict(set)
            note_samples: dict[str, dict[str, Any]] = {}

            for row in sorted(rows, key=lambda item: str(item["observed_at"])):
                feed_id = str(row["feed_id"])
                observed_at = str(row["observed_at"])
                platform_author_id = str(row.get("author_id") or "")
                if not platform_author_id:
                    platform_author_id = "name-" + sha256_text(
                        str(row.get("author") or "")
                    )[:16]
                author_name = str(row.get("author") or "")
                conn.execute(
                    """
                    INSERT INTO authors(
                        platform, platform_author_id, display_name,
                        first_seen_at, last_seen_at
                    ) VALUES ('xiaohongshu', ?, ?, ?, ?)
                    ON CONFLICT(platform, platform_author_id) DO UPDATE SET
                        display_name=CASE WHEN excluded.display_name<>''
                            THEN excluded.display_name ELSE authors.display_name END,
                        last_seen_at=CASE WHEN excluded.last_seen_at>authors.last_seen_at
                            THEN excluded.last_seen_at ELSE authors.last_seen_at END
                    """,
                    (platform_author_id, author_name, observed_at, observed_at),
                )
                author_id = int(
                    conn.execute(
                        """SELECT id FROM authors
                           WHERE platform='xiaohongshu' AND platform_author_id=?""",
                        (platform_author_id,),
                    ).fetchone()["id"]
                )
                title = str(row.get("title") or "")
                liked = max(0, int(row.get("liked_count") or 0))
                collected = max(0, int(row.get("collected_count") or 0))
                comments = max(0, int(row.get("comment_count") or 0))
                shared = max(0, int(row.get("shared_count") or 0))
                conn.execute(
                    """
                    INSERT INTO notes(
                        platform, platform_note_id, author_id, canonical_url,
                        title, note_type, published_at, first_seen_at, last_seen_at,
                        cover_url, content_hash, current_liked_count,
                        current_collected_count, current_comment_count,
                        current_shared_count
                    ) VALUES (
                        'xiaohongshu', ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    ON CONFLICT(platform, platform_note_id) DO UPDATE SET
                        author_id=excluded.author_id,
                        canonical_url=excluded.canonical_url,
                        title=CASE WHEN excluded.title<>'' THEN excluded.title ELSE notes.title END,
                        note_type=excluded.note_type,
                        last_seen_at=CASE WHEN excluded.last_seen_at>notes.last_seen_at
                            THEN excluded.last_seen_at ELSE notes.last_seen_at END,
                        cover_url=CASE WHEN excluded.cover_url<>''
                            THEN excluded.cover_url ELSE notes.cover_url END,
                        content_hash=excluded.content_hash,
                        current_liked_count=excluded.current_liked_count,
                        current_collected_count=excluded.current_collected_count,
                        current_comment_count=excluded.current_comment_count,
                        current_shared_count=excluded.current_shared_count
                    """,
                    (
                        feed_id,
                        author_id,
                        str(row.get("canonical_url") or ""),
                        title,
                        str(row.get("note_type") or ""),
                        observed_at,
                        observed_at,
                        str(row.get("cover_url") or ""),
                        sha256_text(title),
                        liked,
                        collected,
                        comments,
                        shared,
                    ),
                )
                note_id = int(
                    conn.execute(
                        """SELECT id FROM notes
                           WHERE platform='xiaohongshu' AND platform_note_id=?""",
                        (feed_id,),
                    ).fetchone()["id"]
                )
                layer = str(row["layer"])
                database_run_id = database_run_ids[layer]
                query_id = query_ids[str(row["query_key"])]
                conn.execute(
                    """
                    INSERT INTO search_observations(
                        run_id, query_id, note_id, layer, observed_at,
                        result_rank, time_filter, sort_mode
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id, query_id, note_id) DO UPDATE SET
                        observed_at=excluded.observed_at,
                        result_rank=excluded.result_rank,
                        time_filter=excluded.time_filter,
                        sort_mode=excluded.sort_mode
                    """,
                    (
                        database_run_id,
                        query_id,
                        note_id,
                        layer,
                        observed_at,
                        int(row.get("result_rank") or 0),
                        str(row.get("time_filter") or ""),
                        str(row.get("sort_mode") or ""),
                    ),
                )
                snapshot_key = (database_run_id, note_id)
                previous = latest_snapshot.get(snapshot_key)
                if previous is None or observed_at > str(previous["observed_at"]):
                    latest_snapshot[snapshot_key] = {
                        "observed_at": observed_at,
                        "liked": liked,
                        "collected": collected,
                        "comments": comments,
                        "shared": shared,
                    }
                note_sources[feed_id].add(str(row["query_text"]))
                note_samples[feed_id] = {
                    "note_id": note_id,
                    "title": title,
                }

            for (database_run_id, note_id), snapshot in latest_snapshot.items():
                conn.execute(
                    """
                    INSERT INTO note_snapshots(
                        note_id, run_id, observed_at, liked_count, collected_count,
                        comment_count, shared_count, source
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'search')
                    ON CONFLICT(note_id, run_id) DO UPDATE SET
                        observed_at=excluded.observed_at,
                        liked_count=excluded.liked_count,
                        collected_count=excluded.collected_count,
                        comment_count=excluded.comment_count,
                        shared_count=excluded.shared_count,
                        source='search'
                    """,
                    (
                        note_id,
                        database_run_id,
                        snapshot["observed_at"],
                        snapshot["liked"],
                        snapshot["collected"],
                        snapshot["comments"],
                        snapshot["shared"],
                    ),
                )

            for feed_id, sample in note_samples.items():
                sample["source_keywords"] = sorted(note_sources[feed_id])
                for slug, confidence, evidence in classify_note(sample, None, config):
                    conn.execute(
                        """
                        INSERT INTO note_topic_links(
                            note_id, topic_id, confidence, assigned_by, evidence
                        ) VALUES (?, ?, ?, 'rule_v1', ?)
                        ON CONFLICT(note_id, topic_id) DO UPDATE SET
                            confidence=excluded.confidence,
                            assigned_by=excluded.assigned_by,
                            evidence=excluded.evidence
                        WHERE note_topic_links.assigned_by<>'human_coded'
                        """,
                        (sample["note_id"], topic_ids[slug], confidence, evidence),
                    )
        after = table_counts(conn)
        return {
            "collector_run_id": collector_run_id,
            "database_run_ids": database_run_ids,
            "layers": layers,
            "observations_in_file": len(rows),
            "unique_notes_in_file": len({str(row["feed_id"]) for row in rows}),
            "fallback_operations": sum(
                1
                for item in manifest["operation_results"]
                if bool(item.get("fallback_used"))
            ),
            "before": before,
            "after": after,
            "delta": {key: after[key] - before[key] for key in after},
        }
    finally:
        conn.close()


def import_qualified_details(
    db_path: Path, schema_path: Path, run_dir: Path
) -> dict[str, Any]:
    """Persist the strict high-save detail pool without creating new note identities."""
    initialize(db_path, schema_path)
    summary = read_json(run_dir / "qualified-details-v2-summary.json")
    details = read_jsonl(run_dir / "qualified-details-v2.jsonl")
    if summary.get("status") != "completed":
        raise ValueError("Qualified detail collection must be completed before import")
    if len(details) != int(summary["counts"]["selected"]):
        raise ValueError("Qualified detail count does not match its selection")
    collector_run_id = str(summary["run_id"])
    conn = connect(db_path)
    try:
        before = table_counts(conn)
        with conn:
            for detail in details:
                feed_id = str(detail["feed_id"])
                note_row = conn.execute(
                    """SELECT id FROM notes
                       WHERE platform='xiaohongshu' AND platform_note_id=?""",
                    (feed_id,),
                ).fetchone()
                if not note_row:
                    raise ValueError(f"Detail refers to unknown note identity: {feed_id}")
                note_id = int(note_row["id"])
                captured_at = str(detail["captured_at"])
                author_platform_id = str(detail.get("author_id") or "")
                author_name = str(detail.get("author") or "")
                if author_platform_id:
                    conn.execute(
                        """
                        INSERT INTO authors(
                            platform, platform_author_id, display_name,
                            first_seen_at, last_seen_at
                        ) VALUES ('xiaohongshu', ?, ?, ?, ?)
                        ON CONFLICT(platform, platform_author_id) DO UPDATE SET
                            display_name=CASE WHEN excluded.display_name<>''
                                THEN excluded.display_name ELSE authors.display_name END,
                            last_seen_at=CASE WHEN excluded.last_seen_at>authors.last_seen_at
                                THEN excluded.last_seen_at ELSE authors.last_seen_at END
                        """,
                        (author_platform_id, author_name, captured_at, captured_at),
                    )
                    author_id = int(
                        conn.execute(
                            """SELECT id FROM authors WHERE platform='xiaohongshu'
                               AND platform_author_id=?""",
                            (author_platform_id,),
                        ).fetchone()["id"]
                    )
                else:
                    author_id = None
                title = str(detail.get("title") or "")
                description = str(detail.get("description") or "")
                liked = max(0, int(detail.get("liked_count") or 0))
                collected = max(0, int(detail.get("collected_count") or 0))
                comments = max(0, int(detail.get("comment_count") or 0))
                shared = max(0, int(detail.get("shared_count") or 0))
                conn.execute(
                    """
                    UPDATE notes SET
                        author_id=COALESCE(?, author_id),
                        title=CASE WHEN ?<>'' THEN ? ELSE title END,
                        note_type=?,
                        published_at=COALESCE(?, published_at),
                        last_seen_at=CASE WHEN ?>last_seen_at THEN ? ELSE last_seen_at END,
                        content_hash=?,
                        current_liked_count=?,
                        current_collected_count=?,
                        current_comment_count=?,
                        current_shared_count=?
                    WHERE id=?
                    """,
                    (
                        author_id,
                        title,
                        title,
                        str(detail.get("note_type") or ""),
                        iso_from_unix_ms(detail.get("published_at_unix_ms")),
                        captured_at,
                        captured_at,
                        sha256_text(title + "\n" + description),
                        liked,
                        collected,
                        comments,
                        shared,
                        note_id,
                    ),
                )
                selection_reason = (
                    f"{detail.get('qualification_tier')};"
                    f"topic={detail.get('topic_slug')};"
                    f"rank={detail.get('selection_rank')}"
                )
                conn.execute(
                    """
                    INSERT INTO note_details(
                        note_id, captured_at, description, description_hash,
                        image_count, returned_comment_count, selection_reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(note_id) DO UPDATE SET
                        captured_at=excluded.captured_at,
                        description=excluded.description,
                        description_hash=excluded.description_hash,
                        image_count=excluded.image_count,
                        returned_comment_count=excluded.returned_comment_count,
                        selection_reason=excluded.selection_reason
                    """,
                    (
                        note_id,
                        captured_at,
                        description,
                        sha256_text(description),
                        int(detail.get("image_count") or 0),
                        int(detail.get("returned_comment_count") or 0),
                        selection_reason,
                    ),
                )
                for comment in detail.get("comment_signals", []):
                    content = str(comment.get("content") or "").strip()
                    if not content:
                        continue
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO comment_signals(
                            note_id, captured_at, content, content_hash, liked_count
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            note_id,
                            captured_at,
                            content,
                            sha256_text(content),
                            max(0, int(comment.get("liked_count") or 0)),
                        ),
                    )
                conn.execute(
                    """
                    UPDATE note_snapshots SET
                        observed_at=?, liked_count=?, collected_count=?,
                        comment_count=?, shared_count=?, source='detail'
                    WHERE note_id=? AND run_id LIKE ?
                    """,
                    (
                        captured_at,
                        liked,
                        collected,
                        comments,
                        shared,
                        note_id,
                        collector_run_id + "-%",
                    ),
                )
        after = table_counts(conn)
        return {
            "collector_run_id": collector_run_id,
            "details_in_file": len(details),
            "before": before,
            "after": after,
            "delta": {key: after[key] - before[key] for key in after},
        }
    finally:
        conn.close()


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = [
        "radar_runs",
        "authors",
        "notes",
        "search_queries",
        "search_observations",
        "note_snapshots",
        "note_details",
        "comment_signals",
        "topics",
        "note_topic_links",
        "topic_snapshots",
    ]
    return {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in tables
    }


def allocate_budget(total: int, mix: dict[str, float]) -> dict[str, int]:
    raw = {group: total * float(weight) for group, weight in mix.items()}
    result = {group: int(math.floor(value)) for group, value in raw.items()}
    remaining = total - sum(result.values())
    order = sorted(raw, key=lambda group: raw[group] - result[group], reverse=True)
    for group in order[:remaining]:
        result[group] += 1
    return result


def build_weekly_plan(config: dict[str, Any], run_date: str) -> dict[str, Any]:
    date_value = datetime.fromisoformat(run_date).date()
    budget = int(config["weekly_query_budget"])
    group_budget = allocate_budget(budget, config["query_mix"])
    selected: list[dict[str, Any]] = []
    actual_group_budget: dict[str, int] = {}
    seen_keys: set[str] = set()
    seen_texts: set[str] = set()
    for group, count in group_budget.items():
        candidates = [
            query
            for query in config["queries"]
            if query["group"] == group
            and (
                "active_months" not in query
                or date_value.month in query["active_months"]
            )
        ]
        candidates.sort(key=lambda item: (int(item.get("priority", 100)), item["key"]))
        chosen: list[dict[str, Any]] = []
        for query in candidates:
            if len(chosen) >= count:
                break
            text_key = " ".join(query["text"].split()).casefold()
            if query["key"] in seen_keys or text_key in seen_texts:
                continue
            chosen.append(dict(query))
            seen_keys.add(query["key"])
            seen_texts.add(text_key)
        selected.extend(chosen)
        actual_group_budget[group] = len(chosen)

    actual_budget = len(selected)
    topic_budget = allocate_budget(actual_budget, config["weekly_layer_mix"])["topic"]
    topic_candidates = [
        query
        for query in selected
        if "topic" in query["layers"] and query["group"] in {"fixed", "decision"}
    ]
    topic_keys = {query["key"] for query in topic_candidates[:topic_budget]}
    operations: list[dict[str, Any]] = []
    layer_positions: Counter[str] = Counter()
    for index, query in enumerate(selected, start=1):
        layer = "topic" if query["key"] in topic_keys else "recent"
        layer_config = config["layers"][layer]
        variants = layer_config["variants"]
        variant = variants[layer_positions[layer] % len(variants)]
        layer_positions[layer] += 1
        operations.append(
            {
                "operation": index,
                "query_key": query["key"],
                "query_text": query["text"],
                "query_group": query["group"],
                "layer": layer,
                "time_filter": variant["time_filter"],
                "sort_mode": variant["sort_mode"],
                "variant_role": variant["role"],
                "source": query.get("source"),
            }
        )
    plan = {
        "schema_version": "1.0",
        "run_date": run_date,
        "status": "planned",
        "weekly_query_budget": actual_budget,
        "group_budget": actual_group_budget,
        "layer_budget": {
            layer: layer_positions[layer] for layer in config["weekly_layer_mix"]
        },
        "operations": operations,
        "classic_monthly_budget": config["monthly_classic_budget"],
        "notes": [
            "The 40/30/20/10 mix applies to query operations, not unique notes.",
            "Classic-layer operations are a separate monthly run.",
            "Read-only collection must use the existing local collector boundary.",
        ],
    }
    if actual_budget < budget:
        plan["budget_adjustment"] = {
            "reason": "insufficient_active_unique_queries",
            "requested": budget,
            "actual": actual_budget,
            "shortfall": budget - actual_budget,
            "groups": {
                group: {
                    "requested": count,
                    "actual": actual_group_budget[group],
                    "shortfall": count - actual_group_budget[group],
                }
                for group, count in group_budget.items()
            },
        }
        plan["notes"].append(
            f"Requested {budget} query operations; only {actual_budget} active unique "
            "queries fit the group quotas. No expired, duplicate or substitute "
            "queries were added to fill the shortfall. Actual counts are reported."
        )
    return plan


def write_plan(config_path: Path, output_path: Path, run_date: str) -> dict[str, Any]:
    plan = build_weekly_plan(read_json(config_path), run_date)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(plan, ensure_ascii=False, indent=2) + "\n").encode("utf-8-sig")
    with NamedTemporaryFile(dir=output_path.parent, delete=False) as pending:
        temporary = Path(pending.name)
    try:
        temporary.write_bytes(data)
        os.link(temporary, output_path)
    finally:
        temporary.unlink(missing_ok=True)
    return plan


def qualified_report_topics(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Use only explicit topic assignments with >=1000 saves and 3 authors."""
    groups: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for item in candidates:
        author = str(item.get("author_id") or item.get("author") or "")
        slug = str(item.get("topic_slug") or "")
        if not author or not slug or int(item.get("collected_count") or 0) < 1000:
            continue
        previous = groups[slug].get(author)
        if previous is None or int(item["collected_count"]) > int(previous["collected_count"]):
            groups[slug][author] = item
    topics = []
    for slug, authors in groups.items():
        if len(authors) < 3:
            continue
        notes = sorted(authors.values(), key=lambda n: int(n["collected_count"]), reverse=True)
        topics.append({
            "slug": slug, "name": str(notes[0].get("topic_name") or slug),
            "note_count": len(notes), "distinct_author_count": len(authors),
            "median_collected": statistics.median(int(n["collected_count"]) for n in notes),
            "top_notes": [{"title": str(n.get("title") or "无标题"),
                           "author": str(n.get("author") or ""),
                           "collected": int(n["collected_count"]),
                           "url": str(n.get("canonical_url") or "")} for n in notes[:3]],
        })
    return sorted(topics, key=lambda t: (t["distinct_author_count"], t["median_collected"]), reverse=True)


def read_collector_report_observations(
    run_dir: Path, manifest: dict[str, Any]
) -> list[dict[str, Any]]:
    path = run_dir / "observations.jsonl"
    declared = manifest.get("counts", {}).get("observations")
    incomplete_zero = (
        manifest.get("status") in {"failed", "paused", "partial"}
        and type(declared) is int and declared == 0
    )
    if not path.exists() and incomplete_zero:
        # No invented file or records. A failed first operation may never have
        # emitted a JSONL item; the original failure remains in the manifest.
        return []
    return read_jsonl(path)


def analyze_collector_run(
    db_path: Path, config_path: Path, run_dir: Path
) -> dict[str, Any]:
    manifest = read_json(run_dir / "manifest.json")
    rows = read_collector_report_observations(run_dir, manifest)
    collector_run_id = str(manifest["run_id"])
    plan = read_json(Path(str(manifest["plan_path"])))
    plan_by_operation = {
        int(operation["operation"]): operation
        for operation in plan.get("operations", [])
    }
    result_operation_ids = {
        int(item["operation"]) for item in manifest["operation_results"]
    }
    layers = sorted(
        {str(row["layer"]) for row in rows}
        | {
            str(plan_by_operation[operation]["layer"])
            for operation in result_operation_ids
            if operation in plan_by_operation
        }
    )
    run_ids = [f"{collector_run_id}-{layer}" for layer in layers]
    placeholders = ",".join("?" for _ in run_ids)
    fallback_operations = [
        int(item["operation"])
        for item in manifest["operation_results"]
        if bool(item.get("fallback_used"))
    ]

    by_operation: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_operation[int(row["operation"])].append(row)
    query_summaries: list[dict[str, Any]] = []
    for item in sorted(manifest["operation_results"], key=lambda x: int(x["operation"])):
        operation = int(item["operation"])
        operation_rows = by_operation[operation]
        planned = plan_by_operation.get(operation, {})
        observed = operation_rows[0] if operation_rows else {}
        saves = [int(row.get("collected_count") or 0) for row in operation_rows]
        likes = [int(row.get("liked_count") or 0) for row in operation_rows]
        representative = sorted(
            operation_rows,
            key=lambda row: (
                int(row.get("collected_count") or 0),
                int(row.get("liked_count") or 0),
            ),
            reverse=True,
        )[:2]
        query_summaries.append(
            {
                "operation": operation,
                "query": str(observed.get("query_text") or planned.get("query_text") or ""),
                "layer": str(observed.get("layer") or planned.get("layer") or ""),
                "fallback_used": bool(item.get("fallback_used")),
                "time_filter": str(
                    observed.get("time_filter")
                    or item.get("applied_time_filter")
                    or planned.get("time_filter")
                    or ""
                ),
                "sort_mode": str(
                    observed.get("sort_mode")
                    or item.get("applied_sort_mode")
                    or planned.get("sort_mode")
                    or ""
                ),
                "unique_notes": len({str(row["feed_id"]) for row in operation_rows}),
                "median_collected": statistics.median(saves) if saves else 0,
                "median_liked": statistics.median(likes) if likes else 0,
                "representative_notes": [
                    {
                        "title": str(row.get("title") or "无标题"),
                        "author": str(row.get("author") or ""),
                        "collected": int(row.get("collected_count") or 0),
                        "liked": int(row.get("liked_count") or 0),
                        "url": str(row.get("canonical_url") or ""),
                    }
                    for row in representative
                ],
            }
        )

    if (
        not rows
        and manifest.get("status") in {"failed", "paused", "partial"}
        and type(manifest.get("counts", {}).get("observations")) is int
        and manifest["counts"]["observations"] == 0
    ):
        # This report describes a failed collection boundary, not a database
        # analysis. Do not access or infer the contents of the central store.
        return {
            "empty_failed_run": True,
            "collector_run_id": collector_run_id,
            "run_status": str(manifest["status"]),
            "stop_reason": str(manifest.get("stop_reason") or ""),
            "planned_operation_count": len(plan_by_operation),
            "operation_count": sum(item["status"] == "passed" for item in manifest["operation_results"]),
            "failed_operation_count": sum(item["status"] == "failed" for item in manifest["operation_results"]),
            "pending_operation_count": len(set(plan_by_operation) - result_operation_ids),
            "observation_count": 0,
            "unique_note_count": 0,
            "observations_file_missing": not (run_dir / "observations.jsonl").exists(),
            "database_checked": False,
            "network_request_made": False,
            "query_summaries": query_summaries,
            "operation_failures": [
                {
                    "operation": int(item["operation"]),
                    "error_category": str(item.get("error_category") or "unknown"),
                    # Fixed error codes only; do not echo arbitrary URLs, note
                    # text or possible credentials inside an error envelope.
                    "error_codes": re.findall(
                        r"net::ERR_[A-Z_]+|readiness_timeout|site_mismatch",
                        str(item.get("error_message") or ""),
                    ),
                }
                for item in manifest["operation_results"]
                if item["status"] == "failed"
            ],
        }

    # Reporting must never initialize or mutate the central database.
    conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        imported_note_rows = list(
            conn.execute(
                f"""
                SELECT DISTINCT n.id, n.author_id, n.title, n.canonical_url,
                       MAX(s.collected_count) AS current_collected_count,
                       MAX(s.liked_count) AS current_liked_count
                FROM note_snapshots s
                JOIN notes n ON n.id=s.note_id
                WHERE s.run_id IN ({placeholders})
                GROUP BY n.id
                """,
                run_ids,
            )
        )
        imported_note_ids = [int(row["id"]) for row in imported_note_rows]
        baseline_note_ids = {
            int(row[0])
            for row in conn.execute(
                "SELECT note_id FROM note_snapshots WHERE run_id=?",
                (BASELINE_RUN_ID,),
            )
        }
        overlap = set(imported_note_ids) & baseline_note_ids
        historical_note_ids = {
            int(row[0]) for row in conn.execute(
                f"""
                SELECT DISTINCT previous.note_id FROM note_snapshots previous
                JOIN (
                    SELECT note_id, MIN(julianday(observed_at)) AS first_time
                    FROM note_snapshots WHERE run_id IN ({placeholders})
                    GROUP BY note_id
                ) current ON current.note_id=previous.note_id
                WHERE previous.run_id NOT IN ({placeholders})
                  AND julianday(previous.observed_at) < current.first_time
                """, [*run_ids, *run_ids]
            )
        }
        selection_path = run_dir / "detail-selection-v2.json"
        details_path = run_dir / "qualified-details-v2.jsonl"
        summary_path = run_dir / "qualified-details-v2-summary.json"
        selection = read_json(selection_path) if selection_path.exists() else {}
        detail_rows = read_jsonl(details_path) if details_path.exists() else []
        detail_ids = {str(item["feed_id"]) for item in detail_rows}
        detail_summary = read_json(summary_path) if summary_path.exists() else {}
        details_by_id = {str(item["feed_id"]): item for item in detail_rows}
        report_candidates = []
        for item in selection.get("selected", []):
            # Keep this run's topic assignment; prefer actual details for metrics.
            detail = details_by_id.get(str(item["feed_id"]), {})
            report_candidates.append({**item, **detail,
                                      "topic_slug": item.get("topic_slug"),
                                      "topic_name": item.get("topic_name")})
        detail_db_count = int(conn.execute(
            f"""SELECT COUNT(DISTINCT s.note_id) FROM note_snapshots s
                JOIN note_details d ON d.note_id=s.note_id
                WHERE s.run_id IN ({placeholders}) AND s.source='detail'""",
            run_ids,
        ).fetchone()[0])
        interval_hours: list[float] = []
        for note_id in overlap:
            snapshots = list(
                conn.execute(
                    f"""
                    SELECT observed_at FROM note_snapshots
                    WHERE note_id=? AND (run_id=? OR run_id IN ({placeholders}))
                    ORDER BY observed_at
                    """,
                    [note_id, BASELINE_RUN_ID, *run_ids],
                )
            )
            first = parse_time(str(snapshots[0]["observed_at"]))
            last = parse_time(str(snapshots[-1]["observed_at"]))
            if first and last:
                interval_hours.append((last - first).total_seconds() / 3600)

        topic_summaries: list[dict[str, Any]] = []
        if imported_note_ids:
            note_placeholders = ",".join("?" for _ in imported_note_ids)
            for topic in conn.execute("SELECT id, slug, name FROM topics WHERE active=1"):
                topic_rows = list(
                    conn.execute(
                        f"""
                        SELECT DISTINCT n.id, n.author_id, n.title, n.canonical_url,
                               MAX(s.collected_count) AS current_collected_count,
                               MAX(s.liked_count) AS current_liked_count,
                               a.display_name
                        FROM note_topic_links l
                        JOIN notes n ON n.id=l.note_id
                        JOIN note_snapshots s ON s.note_id=n.id
                        LEFT JOIN authors a ON a.id=n.author_id
                        WHERE l.topic_id=? AND n.id IN ({note_placeholders})
                          AND s.run_id IN ({placeholders})
                        GROUP BY n.id
                        """,
                        [int(topic["id"]), *imported_note_ids, *run_ids],
                    )
                )
                if not topic_rows:
                    continue
                if not selection_path.exists():
                    report_candidates.extend({
                        "topic_slug": str(topic["slug"]), "topic_name": str(topic["name"]),
                        "author_id": row["author_id"], "author": row["display_name"],
                        "title": row["title"], "canonical_url": row["canonical_url"],
                        "collected_count": row["current_collected_count"],
                    } for row in topic_rows)
                saves = [int(row["current_collected_count"]) for row in topic_rows]
                top_notes = sorted(
                    topic_rows,
                    key=lambda row: (
                        int(row["current_collected_count"]),
                        int(row["current_liked_count"]),
                    ),
                    reverse=True,
                )[:3]
                topic_summaries.append(
                    {
                        "slug": str(topic["slug"]),
                        "name": str(topic["name"]),
                        "note_count": len(topic_rows),
                        "distinct_author_count": len(
                            {row["author_id"] for row in topic_rows if row["author_id"]}
                        ),
                        "median_collected": statistics.median(saves),
                        "top_notes": [
                            {
                                "title": str(row["title"] or "无标题"),
                                "author": str(row["display_name"] or ""),
                                "collected": int(row["current_collected_count"]),
                                "url": str(row["canonical_url"] or ""),
                            }
                            for row in top_notes
                        ],
                    }
                )
        topic_summaries.sort(
            key=lambda item: (
                item["distinct_author_count"],
                item["note_count"],
                item["median_collected"],
            ),
            reverse=True,
        )
        return {
            "collector_run_id": collector_run_id,
            "run_status": str(manifest["status"]),
            "stop_reason": str(manifest.get("stop_reason") or ""),
            "database_run_ids": run_ids,
            "planned_operation_count": len(plan_by_operation),
            "operation_count": sum(item["status"] == "passed" for item in manifest["operation_results"]),
            "failed_operation_count": sum(item["status"] == "failed" for item in manifest["operation_results"]),
            "pending_operation_count": len(set(plan_by_operation) - result_operation_ids),
            "observation_count": len(rows),
            "unique_note_count": len({str(row["feed_id"]) for row in rows}),
            "imported_note_count": len(imported_note_ids),
            "baseline_overlap_count": len(overlap),
            "historical_overlap_count": len(historical_note_ids),
            "new_note_count": len(imported_note_ids) - len(historical_note_ids),
            "detail_selected_count": len(selection.get("selected", [])),
            "detail_completed_count": len(detail_ids),
            "detail_database_count": detail_db_count,
            "detail_status": str(detail_summary.get("status") or "not_started"),
            "qualified_topic_summaries": qualified_report_topics(report_candidates),
            "topic_assignment_source": "本轮详情候选名单" if selection_path.exists() else "中央库既有标签，需核对研究主题",
            "fallback_operations": fallback_operations,
            "growth_interval_hours_max": round(max(interval_hours), 2)
            if interval_hours
            else None,
            "growth_eligible_note_count": sum(hours >= 24 for hours in interval_hours),
            "query_summaries": query_summaries,
            "topic_summaries": topic_summaries,
            "table_counts": table_counts(conn),
        }
    finally:
        conn.close()


def write_collector_report(
    db_path: Path, config_path: Path, run_dir: Path, output_path: Path
) -> dict[str, Any]:
    analysis = analyze_collector_run(db_path, config_path, run_dir)
    if analysis.get("empty_failed_run"):
        lines = [
            "# 小红书专题雷达批次失败报告", "",
            f"- 采集运行：`{analysis['collector_run_id']}`",
            f"- 搜索状态：`{analysis['run_status']}`；停止原因：`{analysis['stop_reason'] or '未记录'}`。",
            f"- 计划 {analysis['planned_operation_count']} 个查询：完成 {analysis['operation_count']}，失败 {analysis['failed_operation_count']}，未尝试 {analysis['pending_operation_count']}。",
            "- 本轮取得 0 条观测、0 篇笔记；没有可用于专题或爆款判断的样本。",
            "- 未检查中央数据库；本报告只读取本轮清单与计划，未发起搜索、登录或自动恢复。",
            "- 当前登录状态未由本报告核验；网络导航超时不能据此认定登录失效。", "",
            "## 已记录的失败", "",
        ]
        for failure in analysis["operation_failures"]:
            codes = ", ".join(failure["error_codes"]) or "无固定错误码，详见原运行清单"
            lines.append(f"- 操作 {failure['operation']}：分类 `{failure['error_category']}`；{codes}。")
        if analysis["observations_file_missing"]:
            lines.extend(["", "清单明确记录零样本，observations.jsonl 尚未生成；本报告没有补造文件或样本。"])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
        return {"analysis": analysis, "report": str(output_path)}
    lines = [
        "# 小红书专题雷达批次报告",
        "",
        f"- 采集运行：`{analysis['collector_run_id']}`",
        f"- 生成时间：{now_iso()}",
        f"- 搜索状态：`{analysis['run_status']}`；停止原因：`{analysis['stop_reason'] or '未记录'}`。",
        "- 本报告仅分析本地记录；当前登录状态、线上搜索可用性：**未验证**。",
        "",
        "## 搜索结果",
        "",
        f"- 计划 {analysis['planned_operation_count']} 个查询：完成 {analysis['operation_count']}，失败 {analysis['failed_operation_count']}，未尝试 {analysis['pending_operation_count']}；获得 {analysis['observation_count']} 条排序观测、{analysis['unique_note_count']} 篇唯一笔记。",
        f"- 本轮已入库 {analysis['imported_note_count']} 篇；其中历史批次已见 {analysis['historical_overlap_count']} 篇、首次见到 {analysis['new_note_count']} 篇。未入库部分不判定新增。",
        f"- 详情状态：`{analysis['detail_status']}`；选择 {analysis['detail_selected_count']} 篇，本地已完成 {analysis['detail_completed_count']} 篇，本轮详情快照已入库 {analysis['detail_database_count']} 篇。",
        f"- {len(analysis['fallback_operations'])} 个查询在严格筛选超时后透明降级，操作号：{analysis['fallback_operations']}。",
        "- 公开运行目录未保存 xsec token；令牌只在私有本地缓存中供后续详情读取使用。",
        "",
        "## 目前不能说什么",
        "",
        f"与初始基线重复 {analysis['baseline_overlap_count']} 篇；可比较快照的最大间隔（小时）为 {analysis['growth_interval_hours_max'] if analysis['growth_interval_hours_max'] is not None else '无可比较数据'}，满 24 小时的基线重复笔记为 {analysis['growth_eligible_note_count']} 篇。本报告**未计算涨速、未判断加速，不能据此宣称已验证爆款**。",
        "",
        "## 达到收藏与作者门槛的候选专题",
        "",
        f"题材归类来源：{analysis['topic_assignment_source']}。以下仅展示至少 3 位不同作者、每篇收藏至少 1000 的候选，每位作者最多保留 1 篇。达到门槛不等于产品机制已验证。",
        "",
    ]
    if not analysis["qualified_topic_summaries"]:
        lines.extend(["当前分组没有专题满足上述门槛；单篇详情仍可作为个案查看。", ""])
    for index, topic in enumerate(analysis["qualified_topic_summaries"][:6], start=1):
        lines.extend(
            [
                f"### {index}. {topic['name']}",
                "",
                f"- {topic['note_count']} 篇，{topic['distinct_author_count']} 位作者，收藏中位数 {topic['median_collected']:.0f}。",
                "- 高收藏代表笔记：",
            ]
        )
        for note in topic["top_notes"]:
            lines.append(
                f"  - [{note['title']}]({note['url']})｜{note['author']}｜收藏 {note['collected']}"
            )
        lines.append("")
    lines.extend(
        [
            "## 查询审计",
            "",
            "| # | 查询 | 层 | 实际筛选 | 降级 | 收藏中位数 |",
            "|---:|---|---|---|:---:|---:|",
        ]
    )
    for query in analysis["query_summaries"]:
        lines.append(
            f"| {query['operation']} | {query['query']} | {query['layer']} | {query['time_filter']}＋{query['sort_mode']} | {'是' if query['fallback_used'] else '否'} | {query['median_collected']:.0f} |"
        )
    lines.extend(
        [
            "",
            "## 证据边界",
            "",
            "- **事实：**查询、观测和详情完成量以本轮文件为准；入库数量与历史重复以中央数据库为准。历史批次重报不计入后续才出现的重复，互动量使用本轮快照。",
            "- **推断：**多作者反复出现且收藏中位数较高的专题，更值得进入详情分析池。",
            "- **假设：**公开收藏量能近似表示用户的保存使用意图。",
            "- **缺口：**详情采集数量见上方；采集完成不等于已完成内容分析、评论需求验证、作者基线比较或产品试玩。",
            "",
        ]
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8-sig")
    return {"analysis": analysis, "report": str(output_path)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--config", type=Path, required=True)
    plan.add_argument("--run-date", default=datetime.now().date().isoformat())
    plan.add_argument("--plan", type=Path, required=True)
    for action in ("import-run", "import-details", "report-run"):
        invocation = commands.add_parser(action)
        invocation.add_argument("--db", type=Path, required=True)
        invocation.add_argument("--run-dir", type=Path, required=True)
        if action != "import-details":
            invocation.add_argument("--config", type=Path, required=True)
        if action == "report-run":
            invocation.add_argument("--report", type=Path, required=True)
        else:
            invocation.add_argument("--schema", type=Path,
                                    default=Path(__file__).resolve().parents[2] / "schemas/xhs-radar-v1.sql")
    args = parser.parse_args()
    if args.command == "plan":
        result = write_plan(args.config, args.plan, args.run_date)
        print(json.dumps({"plan_path": str(args.plan), "operations": len(result["operations"]), "network_request_made": False}))
        return 0
    if not args.db.is_file():
        parser.error("The shared database must exist on the authorized device.")
    if args.command == "import-run":
        result = import_collector_run(args.db, args.schema, args.config, args.run_dir.resolve())
    elif args.command == "import-details":
        result = import_qualified_details(args.db, args.schema, args.run_dir.resolve())
    else:
        result = write_collector_report(args.db, args.config, args.run_dir.resolve(), args.report)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
