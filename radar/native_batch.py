"""Bind a fixed detail selection to a completed search on an installed research host."""
import hashlib
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
import re
import subprocess
import sys

from seat import reserve, windows_environment
from owned_collector import private_collector


PREPARER_SHA = "b2623f5d71650ad5f7c7d495964021157cbaa883f685706cf31f98e2f2f28df8"


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def selection(run, batch, plan, private, source, notes, *, whole_queue=False):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{5,64}", batch):
        raise ValueError("Use a unique 6-65 character batch name using lowercase letters, numbers or hyphens.")
    if not notes or (not whole_queue and len(notes) > 24) or len(set(notes)) != len(notes):
        raise ValueError("Select 1-24 distinct IDs from the frozen reading queue.")
    manifest = read(source / "manifest.json")
    if (manifest.get("status") != "completed" or manifest.get("run_id") != run
            or str(manifest.get("plan_sha256")).lower() != digest(plan)):
        raise ValueError("Frozen plan or completed run identity mismatch")
    if source.name != run or source.parts[-4:-1] != ("data", "xhs-radar", "runs"):
        raise ValueError("Expected the central radar's data/xhs-radar/runs/<run> directory")
    identities = {nid: set() for nid in notes}
    observations = {nid: [] for nid in notes}
    observed_hash = hashlib.sha256()
    with (source / "observations.jsonl").open("rb") as stream:
        for line in stream:
            observed_hash.update(line)
            if line.strip():
                row = json.loads(line)
                if row["run_id"] != run:
                    raise ValueError("Unexpected source run in public observations")
                if row["feed_id"] in identities:
                    nid = row["feed_id"]
                    identities[nid].add((row.get("author_id"), row.get("canonical_url")))
                    if whole_queue:
                        observations[nid].append({key: row.get(key) for key in
                                                  ("query_text", "observed_at", "title", "liked_count_raw", "collected_count_raw")})
    entries = []
    for nid in notes:
        if not re.fullmatch("[0-9a-f]{24}", nid):
            raise ValueError("Invalid note ID")
        task = read(private / "fixed-queue" / (nid + ".json"))
        if (task["note_id"] != nid or task["run_id"] != run
                or task["canonical_url"] != "https://www.xiaohongshu.com/explore/" + nid
                or task["planned_role"] not in ("high_candidate", "ordinary", "boundary")):
            raise ValueError("Frozen note identity or sampling role mismatch")
        if identities[nid] != {(task["author_id"], task["canonical_url"])}:
            raise ValueError("Original author or URL differs from the frozen reading queue")
        if whole_queue and task.get("card_observations") != observations[nid]:
            raise ValueError("Frozen card observations differ from the source run")
        role = "high_engagement_candidate" if task["planned_role"] == "high_candidate" else "control_candidate"
        entries.append({"feed_id": nid, "sample_role": role, "selection_reason": task["reason"]})
    return {"collector_run_id": run, "source_manifest_sha256": digest(source / "manifest.json"),
            "source_observations_sha256": observed_hash.hexdigest(), "selected": entries}


def check_queue(run, plan, private, source):
    """Read only the frozen plan, fixed tasks, public manifest and observations."""
    queue = private / "fixed-queue"
    if not queue.is_dir():
        raise ValueError("The frozen queue directory is missing.")
    ids = sorted(path.stem for path in queue.glob("*.json"))
    spec = selection(run, "offline-check", plan, private, source, ids, whole_queue=True)
    return {"status": "verified", "entries": len(spec["selected"]), "network_request_made": False}


def native_originals(args, plan, private, shell):
    preparer = Path(__file__).with_name("prepare_radar_detail_batch.py")
    if digest(preparer) != PREPARER_SHA:
        raise ValueError("The packaged preparer differs from the reviewed version.")
    source = Path(args.source_run).resolve()
    spec = selection(args.run, args.batch, plan, private, source, args.note)
    legacy_request = private / ("native-selection-" + args.batch + ".json")
    request = private / "native-requests" / (args.batch + ".json")
    if legacy_request.is_file() and read(legacy_request) != spec:
        raise ValueError("The existing S2 batch has a different frozen selection.")
    batch_root = private.parent / "native-batches"
    dest = batch_root / args.run / args.batch
    # Previously prepared S2 batches contain absolute bindings to their original location.
    legacy_root = os.environ.get("S2_RESEARCH_ROOT")
    if legacy_root:
        legacy = Path(legacy_root).resolve() / ".runtime/s2" / args.run / args.batch
        if legacy.exists():
            if dest.exists():
                raise ValueError("Two private checkpoints exist for this batch. Inspect before resuming.")
            dest = legacy
    elif legacy_request.exists() and not dest.exists():
        raise ValueError("Existing batch selection has no local receipt. Set S2_RESEARCH_ROOT to the original research host.")
    receipt = dest / "adapter-receipt.json"
    if not receipt.is_file():
        if legacy_request.exists():
            raise ValueError("The original S2 batch receipt is missing. Check its private root before retrying.")
        request.parent.mkdir(exist_ok=True)
        with NamedTemporaryFile(dir=request.parent, delete=False) as pending:
            temp = Path(pending.name)
        try:
            temp.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
            try:
                os.link(temp, request)
            except FileExistsError:
                if read(request) != spec:
                    raise ValueError("Existing frozen batch has different inputs.")
        finally:
            temp.unlink(missing_ok=True)
        if dest.exists():
            raise ValueError("Incomplete local preparation. Review this batch's files before retrying.")
        command = [sys.executable, "-B", "-X", "utf8", str(preparer), "--selection", str(request),
                   "--run-id", args.run, "--batch-id", args.batch, "--output", str(dest),
                   "--engine-root", str(source.parents[3]), "--private-root", str(batch_root)]
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL)
    binding = read(receipt)
    script = dest / "collect-s2-batch.ps1"
    if (binding["run_id"] != args.run or binding["batch_id"] != args.batch
            or binding["adapter_sha256"] != digest(script)
            or binding["selection_sha256"] != digest(dest / "detail-selection-v2.json")
            or read(dest / "selection-request.json") != spec):
        raise ValueError("Prepared native adapter changed")
    owned = private_collector(script, binding["adapter_sha256"], private / "owner-bound", "native")
    result = {"stage": "prepared", "selected": len(args.note), "run": args.run,
              "batch": args.batch, "private_output_directory": str(dest), "private_collector": str(owned),
              "network_request_made": False}
    if not args.execute:
        return result
    summary = dest / "qualified-details-v2-summary.json"
    if summary.is_file():
        state = read(summary)
        if state.get("status") == "completed":
            result["stage"] = "already_completed"
            return result
        if not args.resume:
            raise ValueError("An earlier attempt has a checkpoint. Inspect it before explicitly resuming.")
    if not shell:
        raise ValueError("Windows PowerShell is required for the installed collector.")
    command = [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
               str(owned), "-RunId", args.run, "-DelaySeconds", str(args.delay)]
    if args.resume:
        command.append("-Resume")
    with reserve():
        mark = private / ("native-attempt-" + args.batch)
        if not args.resume:
            with mark.open("x", encoding="utf-8") as f:
                f.write(binding["selection_sha256"])
        code = subprocess.run(command, env=windows_environment()).returncode
    state = read(summary) if summary.is_file() else {}
    result.pop("network_request_made")
    result.update(stage=state.get("status", "no_collector_receipt"), collector_exit=code)
    return result
