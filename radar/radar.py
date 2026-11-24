"""Shared-device adapter for the read-only radar."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from datetime import date
from tempfile import NamedTemporaryFile
from provider import capabilities, read_original
from native_batch import native_originals, check_queue
from owned_collector import private_wrapper
from seat import reserve, windows_environment


PROJECT = Path(__file__).resolve().parents[1]
RUN_ID = re.compile(r"muse-[a-z0-9][a-z0-9-]{1,74}")
DEVICE_WRAPPER_SHA = "373cfd10847168ade74836900eb392512c0d55039366cad72f00300984821434"
REVIEWED_WRAPPER = "e897c2d247087860232dd7c11868fd903f65d9f6c90e41bb202d2bbda1da3c9c"
PACKAGED_WRAPPER = PROJECT / "scripts/run-radar.ps1"


def private_root():
    configured = os.environ.get("S2_MUSE_RADAR_STATE")
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share")
    root = Path(configured).expanduser() if configured else base / "s2-muse" / "radar"
    if not root.is_absolute():
        raise ValueError("S2_MUSE_RADAR_STATE must be an absolute private directory.")
    root = root.resolve()
    if root == PROJECT or PROJECT in root.parents:
        raise ValueError("Keep radar plans outside the Git project.")
    return root


def run_dir(run_id):
    if not RUN_ID.fullmatch(run_id):
        raise ValueError("Use a dedicated muse- run ID (lowercase letters, digits, hyphens).")
    return private_root() / run_id


def make_plan(args):
    directory = run_dir(args.run)
    keywords = [term.strip() for term in args.keyword]
    if any(not term for term in keywords) or len(keywords) != len(set(keywords)):
        raise ValueError("Each search term must be nonempty and unique within this plan.")
    operations = []
    for index, term in enumerate(keywords, 1):
        key = hashlib.sha256(term.encode()).hexdigest()[:24]
        operations.append({
            "operation": index, "query_key": "muse-" + key, "query_text": term,
            "query_group": "exploration", "layer": "recent", "time_filter": args.time_filter,
            "sort_mode": args.sort, "variant_role": "discovery", "source": "s2-muse",
        })
    plan = {"schema_version": "1.0", "run_date": date.today().isoformat(),
            "operations": operations, "weekly_query_budget": len(operations)}
    data = json.dumps(plan, ensure_ascii=False, indent=2).encode("utf-8-sig")
    root = directory.parent
    root.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=root, delete=False) as tmp:
        temp_path = Path(tmp.name)
    try:
        temp_path.write_bytes(data)
        directory.mkdir(exist_ok=False)
        temp_path.replace(directory / ("plan-" + hashlib.sha256(data).hexdigest() + ".json"))
    finally:
        temp_path.unlink(missing_ok=True)
    print(json.dumps({"run_id": args.run, "operations_planned": len(operations),
                      "network_request_made": False}))


def load_plan(run_id):
    files = list(run_dir(run_id).glob("plan-*.json"))
    if len(files) != 1:
        raise ValueError("Expected one local frozen plan for this Muse run.")
    path = files[0]
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if path.name != "plan-" + digest + ".json":
        raise ValueError("The frozen search plan changed.")
    return path


def queue_original(args):
    """Bind one S2 reading task to this search's public observations, entirely offline."""
    plan = load_plan(args.run)
    run = Path(args.source_run).resolve()
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8-sig"))
    if (manifest.get("run_id") != args.run or manifest.get("status") != "completed"
            or manifest.get("plan_sha256", "").lower() != hashlib.sha256(plan.read_bytes()).hexdigest()):
        raise ValueError("Expected this Muse run's completed manifest and unchanged plan digest.")
    if not re.fullmatch(r"[0-9a-f]{24}", args.note):
        raise ValueError("Expected a 24-character public note ID.")
    observed_path = run / "observations.jsonl"
    with observed_path.open(encoding="utf-8-sig") as source:
        matches = []
        for line in source:
            if line.strip():
                row = json.loads(line)
                if row.get("run_id") == args.run and row.get("feed_id") == args.note:
                    matches.append(row)
    authors = {row.get("author_id") for row in matches}
    canonical = "https://www.xiaohongshu.com/explore/" + args.note
    if len(authors) != 1 or not all(authors) or any(r.get("canonical_url") != canonical for r in matches):
        raise ValueError("Absent note, unknown/conflicting author, or inconsistent public URL.")
    record = {
        "run_id": args.run, "note_id": args.note, "author_id": authors.pop(), "canonical_url": canonical,
        "planned_role": args.role, "status": "unread", "reason": args.reason,
        "card_observations": [{key: row.get(key) for key in (
            "query_text", "observed_at", "title", "liked_count_raw", "collected_count_raw"
        )} for row in matches],
    }
    directory = run_dir(args.run) / "fixed-queue"
    directory.mkdir(exist_ok=True)
    target = directory / (args.note + ".json")
    with NamedTemporaryFile(dir=directory, delete=False) as stream:
        pending = Path(stream.name)
    try:
        pending.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
        # Atomic create; keep an existing queue item immutable across concurrent writers.
        if os.name == "nt":
            pending.rename(target)
        else:
            os.link(pending, target)
    finally:
        pending.unlink(missing_ok=True)
    print(json.dumps({"run_id": args.run, "entry": target.name, "status": "unread",
                      "network_request_made": False}))


def wrapper_command():
    default = PACKAGED_WRAPPER
    wrapper = Path(os.environ.get("XHS_RADAR_WRAPPER") or default).expanduser().resolve()
    if not wrapper.is_file():
        raise ValueError("Install the default radar on this host or set XHS_RADAR_WRAPPER.")
    if hashlib.sha256(wrapper.read_bytes()).hexdigest() not in (REVIEWED_WRAPPER, DEVICE_WRAPPER_SHA):
        raise ValueError("The installed radar wrapper differs from the reviewed version; review its interface before use.")
    shell = shutil.which("powershell.exe")
    if not shell:
        raise ValueError("The installed radar requires a Windows host with Windows PowerShell.")
    # Match the installed Skill's invocation, scoped to this process and pinned script.
    return [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)]


def host_status(wrapper):
    result = subprocess.run(wrapper + ["-Action", "status"], capture_output=True, env=windows_environment())
    if result.returncode:
        raise ValueError("Default radar status failed; verify the host engine and its Python runtime.")
    return json.loads(result.stdout.decode("utf-8-sig"))


def run_radar(args):
    if args.action == "status":
        # The host emits local paths and the latest shared run ID. Only expose the capability result.
        source = host_status(wrapper_command())
        print(json.dumps({key: source[key] for key in (
            "status", "engine_ready", "central_database_present", "authentication_state",
            "network_request_made", "mutation_tools_allowed")}))
        return 0
    plan = load_plan(args.run)
    if args.action == "inspect":
        print(plan.read_text(encoding="utf-8-sig"))
        return 0
    wrapper = wrapper_command()
    command = wrapper + ["-Action", args.action, "-RunId", args.run]
    if args.action in ("search", "resume"):
        command.extend(["-PlanPath", str(plan), "-DelaySeconds", str(args.delay)])
    if args.action == "details":
        command.extend(["-DetailBatchSize", "1", "-DelaySeconds", str(args.delay)])
        if args.resume_details:
            command.append("-ResumeDetails")
    # No shell, alternate accounts, automatic retries or access to private provider lookup.
    if args.action in ("search", "resume", "details"):
        with reserve():
            engine = Path(host_status(wrapper)["engine_root"])
            wrapper_path = Path(wrapper[-1])
            wrapper_hash = hashlib.sha256(wrapper_path.read_bytes()).hexdigest()
            if wrapper_hash not in (REVIEWED_WRAPPER, DEVICE_WRAPPER_SHA):
                raise ValueError("Wrapper binding changed before collection.")
            code_root = wrapper_path.parent.parent if wrapper_hash == REVIEWED_WRAPPER else engine
            guarded = private_wrapper(wrapper_path, wrapper_hash, code_root, run_dir(args.run) / "owner-bound",
                                      "details" if args.action == "details" else "search")
            command[len(wrapper) - 1] = str(guarded)
            return subprocess.run(command, env=windows_environment()).returncode
    return subprocess.run(command, env=windows_environment()).returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("status", help="offline check of the installed default radar")
    actions.add_parser("seat-status", help="offline check for a competing radar process or occupied port")
    actions.add_parser("provider-status", help="probe the exact-note reader offline")
    plan = actions.add_parser("plan", help="freeze one private topic-specific search plan")
    plan.add_argument("--run", required=True)
    plan.add_argument("--keyword", action="append", required=True)
    plan.add_argument("--sort", choices=["综合", "最新", "最多点赞", "最多评论", "最多收藏"], default="最新")
    plan.add_argument("--time-filter", choices=["不限", "一天内", "一周内", "半年内"], default="一周内")
    check = actions.add_parser("check-queue", help="verify a frozen fixed queue against its source run offline")
    check.add_argument("--run", required=True)
    check.add_argument("--source-run", required=True)
    queue = actions.add_parser("queue", help="freeze a note from a public search run for original reading")
    queue.add_argument("--run", required=True)
    queue.add_argument("--source-run", required=True, help="run_directory from the local radar receipt")
    queue.add_argument("--note", required=True)
    queue.add_argument("--role", required=True, choices=["high_candidate", "ordinary", "boundary"])
    queue.add_argument("--reason", required=True)
    original = actions.add_parser("read-original", help="read one queued original using the configured host tool")
    original.add_argument("--run", required=True)
    original.add_argument("--note", required=True)
    original.add_argument("--retry", action="store_true")
    native = actions.add_parser("native-originals", help="prepare or execute a fixed same-search native detail batch")
    native.add_argument("--run", required=True)
    native.add_argument("--source-run", required=True, help="central search run directory")
    native.add_argument("--batch", required=True, help="unique detail selection name within this run")
    native.add_argument("--note", action="append", required=True)
    native.add_argument("--execute", action="store_true", help="allow one real native collection attempt")
    native.add_argument("--resume", action="store_true", help="continue the inspected original checkpoint")
    native.add_argument("--delay", type=int, default=90)
    for name in ("inspect", "search", "resume", "details", "report"):
        action = actions.add_parser(name)
        action.add_argument("--run", required=True)
        if name in ("search", "resume", "details"):
            action.add_argument("--delay", type=int, default=90, metavar="SECONDS")
        if name == "details":
            action.add_argument("--resume-details", action="store_true")
    args = parser.parse_args()
    if getattr(args, "delay", 90) < 60:
        parser.error("This low-frequency handoff uses at least 60 seconds between provider operations.")
    try:
        if args.action == "seat-status":
            with reserve():
                print(json.dumps({"shared_seat": "idle", "network_request_made": False}))
            return 0
        if args.action == "plan":
            make_plan(args)
            return 0
        if args.action == "check-queue":
            print(json.dumps(check_queue(args.run, load_plan(args.run), run_dir(args.run), Path(args.source_run).resolve())))
            return 0
        if args.action == "queue":
            queue_original(args)
            return 0
        if args.action == "provider-status":
            print(json.dumps(capabilities()))
            return 0
        if args.action == "native-originals":
            if args.resume and not args.execute:
                raise ValueError("Combine --resume with --execute to resume the inspected checkpoint.")
            result = native_originals(args, load_plan(args.run), run_dir(args.run), shutil.which("powershell.exe"))
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result["stage"] in ("prepared", "already_completed", "completed") else 2
        if args.action == "read-original":
            load_plan(args.run)
            result = read_original(args, run_dir(args.run), PROJECT)
            print(json.dumps(result))
            return 0 if result["status"] in ("reused", "captured") else 2
        return run_radar(args)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
