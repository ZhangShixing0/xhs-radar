"""Replay a small, synthetic radar batch with the repository's offline engine."""

import argparse
import csv
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from html import escape
from pathlib import Path
from tempfile import TemporaryDirectory


KEYWORDS = ["考研数学真题", "考研数学错题本", "考研数学冲刺"]
STAMP = "2026-11-24T10:00:00+08:00"
RUN = "synthetic-batch-20261124"
# Fictional notes: ID, title, author alias, likes, saves and matching query numbers.
NOTES = [
    ("N01", "冲刺前，我把数学真题错题本压成了三页", "A", 936, 1280, (1, 2, 3)),
    ("N02", "数学真题二刷，我只往错题本里记这两行", "B", 702, 960, (1, 2)),
    ("N03", "冲刺最后四周，高数和线代怎么分时间", "C", 564, 735, (3,)),
    ("N04", "做真题总错同类积分题，我在错题本上画了张对照图", "D", 388, 527, (1, 2)),
    ("N05", "冲刺套卷总写不完，我拿两套真题练取舍", "E", 254, 410, (1, 3)),
    ("N06", "冲刺时间紧，我把错题本按这两类重新排了一遍", "F", 216, 305, (2, 3)),
]


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def prepare_receipt(run):
    operations = []
    with (run / "observations.jsonl").open("w", encoding="utf-8", newline="\n") as raw:
        for number, keyword in enumerate(KEYWORDS, 1):
            op = dict(operation=number, layer="recent", query_key=f"synthetic-{number}",
                      query_text=keyword, query_group="exploration", sort_mode="最多收藏", time_filter="一周内")
            operations.append(op)
            matches = [note for note in NOTES if number in note[-1]]
            for rank, (note_id, title, author, likes, saves, _) in enumerate(matches, 1):
                row = dict(op, run_id=RUN, data_kind="synthetic", observed_at=STAMP, result_rank=rank,
                           feed_id=note_id, title=title, author_id=f"synthetic-{author}",
                           author=f"示例账号 {author}", note_type="normal", liked_count=likes,
                           collected_count=saves, example_source=f"sources.html#{note_id}")
                raw.write(json.dumps(row, ensure_ascii=False) + "\n")
    plan = run / "plan.json"
    write_json(plan, {"operations": operations})
    write_json(run / "manifest.json", {
        "run_id": RUN, "status": "completed", "started_at": STAMP, "completed_at": STAMP,
        "plan_path": str(plan), "plan_sha256": hashlib.sha256(plan.read_bytes()).hexdigest(),
        "counts": {"operations_selected": len(operations), "operations_passed": len(operations),
                   "observations": sum(len(note[-1]) for note in NOTES)},
        "operation_results": [{"operation": op["operation"], "status": "passed"} for op in operations],
    })


def write_sample(db, run_id, export):
    sql = """
        SELECT n.platform_note_id, n.title, a.display_name, s.liked_count, s.collected_count,
               s.observed_at, q.query_text, o.result_rank
        FROM search_observations o
        JOIN notes n ON o.note_id = n.id
        JOIN authors a ON a.id = n.author_id
        JOIN search_queries q ON o.query_id = q.id
        JOIN note_snapshots s ON o.note_id = s.note_id AND o.run_id = s.run_id
        WHERE o.run_id = ?
        ORDER BY s.collected_count DESC, n.platform_note_id, q.query_key
    """
    notes = {}
    with closing(sqlite3.connect(db)) as conn:
        for note_id, title, author, likes, saves, stamp, keyword, rank in conn.execute(sql, (run_id,)):
            notes.setdefault((note_id, title, author, likes, saves, stamp), []).append((keyword, rank))

    sections = []
    with (export / "samples.csv").open("w", newline="", encoding="utf-8-sig") as out:
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(["数据性质", "笔记ID", "标题", "作者", "点赞", "收藏", "命中关键词", "采样时间", "本地原帖样本"])
        for (note_id, title, author, likes, saves, stamp), matches in notes.items():
            writer.writerow(["合成示例导出", note_id, title, author, likes, saves,
                             " / ".join(word for word, _ in matches), stamp, f"sources.html#{note_id}"])
            items = "".join(f"<li>{escape(word)} · 本词第 {rank} 条</li>" for word, rank in matches)
            sections.append(f'<section id="{escape(note_id)}"><h2>{escape(title)}</h2><p>{escape(note_id)} · '
                            f'{escape(author)} · {likes} 赞 · {saves} 收藏</p><ul>{items}</ul></section>')
    (export / "sources.html").write_text(
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" '
        'content="width=device-width,initial-scale=1"><title>合成笔记样本</title>'
        '<style>body{max-width:48rem;margin:2rem auto;padding:0 1rem;font:1rem/1.6 system-ui,sans-serif}'
        'section{border-top:1px solid #ddd;margin:1rem 0;padding:1rem 0}section:target{background:#eef4ff}</style>'
        '<h1>合成笔记样本</h1><p>作者、标题和赞藏为虚构数据。每篇下面列出本批次的命中词和结果位置。</p>'
        + "\n".join(sections) + "</html>\n", encoding="utf-8", newline="\n")
    return len(notes)


def replay(repo, work, export):
    run = work / RUN
    run.mkdir()
    prepare_receipt(run)
    db = work / "synthetic.sqlite3"
    config = work / "config.json"
    write_json(config, {"queries": [], "topic_rules": []})
    with closing(sqlite3.connect(db)) as conn:
        conn.executescript((repo / "schemas/xhs-radar-v1.sql").read_text(encoding="utf-8-sig"))

    def call(action, *extra):
        cmd = [sys.executable, "-B", "-X", "utf8", str(repo / "scripts/xhs-radar/xhs_radar.py"), action,
               "--db", str(db), "--config", str(config), "--run-dir", str(run), *map(str, extra)]
        return json.loads(subprocess.check_output(cmd))

    imported = call("import-run")
    if any(call("import-run")["delta"].values()):
        raise RuntimeError("Unexpected changes after importing the same run twice")
    audit = call("report-run", "--report", work / "report.md")["analysis"]
    count = write_sample(db, imported["database_run_ids"]["recent"], export)
    if (audit["operation_count"], audit["observation_count"], audit["unique_note_count"], count) != (3, 12, 6, 6):
        raise RuntimeError("Synthetic batch did not match the expected 3 queries, 12 observations and 6 notes")
    shutil.copyfile(run / "observations.jsonl", export / "observations.jsonl")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="Destination directory for the example exports")
    options = parser.parse_args()
    repo = Path(__file__).resolve().parent.parent
    if not (repo / "scripts/xhs-radar/xhs_radar.py").is_file():
        parser.error("Keep examples/replay.py in the repository alongside scripts/ and schemas/.")
    out = options.out.absolute()
    out.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".radar-replay-", dir=out.parent) as tmp:
        work = Path(tmp)
        export = work / "export"
        export.mkdir()
        replay(repo, work, export)
        try:
            export.rename(out)
        except OSError:
            if not out.exists():
                raise
            for item in export.iterdir():
                target = out / item.name
                if not target.is_file() or target.read_bytes() != item.read_bytes():
                    parser.error(f"Destination already exists with different data: {out}. Choose a new --out directory.")
    print("3 queries | 12 observations | 6 distinct notes | offline")
    print(f"Files in {options.out}: observations.jsonl, samples.csv, sources.html")


if __name__ == "__main__":
    main()
