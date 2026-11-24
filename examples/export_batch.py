"""Export one radar search receipt as one CSV row per note. Python 3.10+."""

import argparse
import csv
import json
import os
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile


COLUMNS = ["笔记ID", "标题", "作者", "点赞", "收藏", "命中关键词", "采样时间", "原帖链接"]
HEX_ID = re.compile(r"[0-9a-fA-F]{24}")
STAMP = re.compile(r"(\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d)(?:[.,](\d+))?(Z|[+-]\d\d:\d\d)")


def order_key(value):
    """Preserve even the seventh fractional digit from Windows timestamps."""
    match = STAMP.fullmatch(value)
    if not match:
        raise ValueError("expected a timestamp with a timezone")
    seconds, fraction, offset = match.groups()
    utc = datetime.fromisoformat(seconds + offset.replace("Z", "+00:00")).astimezone(timezone.utc)
    return utc, (fraction or "").rstrip("0")


def count(row, key):
    value = row.get(key)
    if value is None or value == "":
        return ""
    if type(value) is not int or value < 0:
        raise ValueError("invalid count")
    # The native collector also writes zero for blank or unparseable display text.
    raw_key = key + "_raw"
    if value == 0 and raw_key in row:
        raw = str(row[raw_key]).strip()
        if not re.fullmatch(r"0+(?:\.0+)?[万亿]?\+?", raw):
            return ""
    return value


def safe_cell(value):
    if not isinstance(value, str):
        return value
    probe = re.sub(r"^[\s\x00-\x1f\ufeff\u200b-\u200f\u202a-\u202e\u2060]*", "",
                   unicodedata.normalize("NFKC", value))
    if value.startswith(("\t", "\r", "\n")) or probe.startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def read_notes(source):
    notes = {}
    batch = None
    with source.open(encoding="utf-8-sig") as stream:
        for line_no, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                run, note, keyword, stamp = (row[k] for k in ("run_id", "feed_id", "query_text", "observed_at"))
                if not all(isinstance(v, str) and v.strip() for v in (run, note, keyword, stamp)):
                    raise ValueError("missing identity")
                if batch is not None and run != batch:
                    raise ValueError("mixed run IDs")
                batch = run
                real_format = bool(HEX_ID.fullmatch(note))
                if real_format:
                    note = note.lower()
                instant = order_key(stamp)
                entry = notes.setdefault(note, {"keywords": {}, "latest": None})
                entry["keywords"][keyword] = None
                if entry["latest"] is None or instant > entry["latest"]:
                    title, author = (row.get(k) or "" for k in ("title", "author"))
                    if not isinstance(title, str) or not isinstance(author, str):
                        raise ValueError("invalid text field")
                    synthetic = row.get("data_kind") == "synthetic" or run.startswith("synthetic-")
                    link = f"https://www.xiaohongshu.com/explore/{note}" if real_format and not synthetic else ""
                    entry.update(latest=instant, snapshot=(title, author, count(row, "liked_count"),
                                 count(row, "collected_count"), stamp, link))
            except (ValueError, KeyError, TypeError, OverflowError):
                raise ValueError(f"Invalid observation or mixed run IDs at line {line_no}.") from None
    if not notes:
        raise ValueError("No observations to export.")
    return notes


def export(source, target):
    notes = read_notes(source)
    pending = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8-sig", newline="",
                                dir=target.parent, delete=False) as out:
            pending = Path(out.name)
            writer = csv.writer(out)
            writer.writerow(COLUMNS)
            for note, data in notes.items():
                title, author, likes, saves, stamp, link = data["snapshot"]
                # Excel automatically rounds long numeric-only IDs when reading CSV.
                numeric_id = note.isascii() and note.isdigit() and (len(note) > 15 or note.startswith("0"))
                visible_id = "'" + note if numeric_id else note
                writer.writerow(map(safe_cell, (visible_id, title, author, likes, saves,
                                               " / ".join(data["keywords"]), stamp, link)))
        os.link(pending, target)  # Publish the complete file only if the name is still free.
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)
    return len(notes)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("observations", type=Path)
    parser.add_argument("output", type=Path, help="new CSV file in an existing directory")
    args = parser.parse_args()
    try:
        total = export(args.observations, args.output)
    except FileExistsError:
        parser.exit(1, "Output already exists. Choose a new filename.\n")
    except (ValueError, OSError) as err:
        message = err.strerror if isinstance(err, OSError) else str(err)
        parser.exit(1, f"Export failed: {message}\n")
    print(f"Exported {total} distinct notes.")
