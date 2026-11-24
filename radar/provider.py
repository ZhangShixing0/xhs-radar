"""Device-local interface for exact-note reads through the approved radar session."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from datetime import datetime, timezone


CAPABILITIES = ("default_radar_session", "read_only", "exact_note", "local_image_files", "shared_session_lease")
STOP_STATES = {"unread", "partial", "authentication_required", "session_unknown",
               "rate_limited", "verification_required", "unavailable", "timed_out"}


def invoke(request, seconds):
    config = os.environ.get("S2_MUSE_ORIGINAL_PROVIDER")
    if not config:
        raise ValueError("exact_note_provider_unconfigured")
    argv = json.loads(config)
    if (not isinstance(argv, list) or not argv or not all(isinstance(v, str) and v for v in argv)
            or not Path(argv[0]).is_absolute() or not Path(argv[0]).is_file()):
        raise ValueError("S2_MUSE_ORIGINAL_PROVIDER requires a local executable and its argument list.")
    try:
        result = subprocess.run(argv, input=json.dumps(request, ensure_ascii=False).encode(),
                                capture_output=True, timeout=seconds)
    except subprocess.TimeoutExpired:
        raise ValueError("provider_timeout_session_unknown") from None
    if result.returncode:
        raise ValueError("provider_exit_failure")
    data = json.loads(result.stdout.decode("utf-8-sig"))
    if not isinstance(data, dict) or data.get("protocol") != "s2-original/1":
        raise ValueError("provider_protocol_mismatch")
    return data


def capabilities():
    request = {"protocol": "s2-original/1", "action": "capabilities", "network_allowed": False}
    data = invoke(request, 15)
    caps = data.get("capabilities")
    if not isinstance(caps, dict) or data.get("network_request_made") is not False:
        raise ValueError("provider_offline_probe_invalid")
    if any(caps.get(name) is not True for name in CAPABILITIES):
        raise ValueError("provider_lacks_required_capabilities")
    return {"configured": True, "capabilities": list(CAPABILITIES), "live_tested": False,
            "online_session": "unknown", "network_request_made": False}


def validate_capture(data, task, media_root):
    if data.get("note_id") != task["note_id"] or data.get("author_id") != task["author_id"]:
        raise ValueError("original_identity_conflict")
    if data.get("canonical_url") != task["canonical_url"] or data.get("media_type") != "photo":
        raise ValueError("original_url_or_type_mismatch")
    for field in ("title", "body", "published_at", "observed_at"):
        if not isinstance(data.get(field), str):
            raise ValueError("missing_original_field_" + field)
    for field in ("published_at", "observed_at"):
        stamp = datetime.fromisoformat(data[field].replace("Z", "+00:00"))
        if not stamp.tzinfo:
            raise ValueError("original_time_zone_unknown")
    metrics = data.get("metrics")
    if not isinstance(metrics, dict) or not {"likes_raw", "saves_raw"} <= metrics.keys():
        raise ValueError("missing_raw_metric_fields")
    if any(type(metrics[key]) not in (str, int, type(None)) for key in ("likes_raw", "saves_raw")):
        raise ValueError("invalid_raw_metric_fields")
    pages = data.get("pages")
    if (not isinstance(pages, list) or not pages or type(data.get("expected_pages")) is not int
            or len(pages) != data["expected_pages"]):
        raise ValueError("incomplete_original_album")
    for index, page in enumerate(pages, 1):
        if not isinstance(page, dict) or page.get("index") != index or not isinstance(page.get("file"), str):
            raise ValueError("invalid_original_page_index")
        relative = Path(page["file"])
        path = (media_root / relative).resolve()
        if relative.is_absolute() or media_root not in path.parents or not path.is_file():
            raise ValueError("missing_local_evidence_page")
        with path.open("rb") as stream:
            header = stream.read(32)
            is_image = (header.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a"))
                        or header[:4] == b"RIFF" and header[8:12] == b"WEBP"
                        or header[4:8] == b"ftyp" and header[8:12] in
                        (b"avif", b"avis", b"heic", b"heix", b"mif1"))
            if not is_image:
                raise ValueError("unsupported_original_image_signature")
            signature = hashlib.sha256(header)
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                signature.update(block)
        if signature.hexdigest() != page.get("sha256"):
            raise ValueError("original_page_hash_mismatch")


def read_original(args, private_run, project):
    if not re.fullmatch(r"[0-9a-f]{24}", args.note):
        raise ValueError("Invalid note ID.")
    task = json.loads((private_run / "fixed-queue" / (args.note + ".json")).read_text(encoding="utf-8"))
    location = os.environ.get("S2_MUSE_RADAR_MEDIA_ROOT")
    if not location or not Path(location).is_absolute():
        raise ValueError("Configure the private absolute S2_MUSE_RADAR_MEDIA_ROOT on this device.")
    root = Path(location).resolve()
    if not root.is_dir() or project == root or project in root.parents or root in project.parents:
        raise ValueError("Use a dedicated local evidence store outside the Git project.")
    folder = private_run / "originals"
    folder.mkdir(exist_ok=True)
    accepted = folder / (args.note + ".json")
    if accepted.exists():
        validate_capture(json.loads(accepted.read_text(encoding="utf-8")), task, root)
        return {"status": "reused", "network_request_made": False, "visually_read_pages": 0}
    capabilities()
    lock = folder / (args.note + ".lock")
    with lock.open("x", encoding="utf-8") as claim:
        claim.write(datetime.now(timezone.utc).isoformat())
    try:
        attempted = folder / (args.note + ".attempt")
        if attempted.exists() and not args.retry:
            raise ValueError("Previous attempt recorded. Review its checkpoint before explicit --retry.")
        attempted.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
        try:
            data = invoke({"protocol": "s2-original/1", "action": "read_original", "note_id": args.note,
                           "author_id": task["author_id"], "canonical_url": task["canonical_url"],
                           "run_id": args.run, "media_root": str(root), "reuse_existing": True,
                           "allow_video": False, "max_notes": 1}, 180)
        except (ValueError, OSError) as exc:
            code = str(exc)
            attempted.write_text(code if re.fullmatch(r"provider_[a-z_]+", code) else "provider_response_error",
                                 encoding="utf-8")
            raise
        status = data.get("status")
        if status in STOP_STATES:
            attempted.write_text(status, encoding="utf-8")
            return {"status": status, "visually_read_pages": 0}
        if status != "captured":
            attempted.write_text("unknown_original_provider_status", encoding="utf-8")
            raise ValueError("unknown_original_provider_status")
        try:
            validate_capture(data, task, root)
        except (ValueError, OSError) as exc:
            code = str(exc)
            attempted.write_text(code if re.fullmatch(r"[a-z_]+", code) else "invalid_original_evidence",
                                 encoding="utf-8")
            raise
        # Save only the documented public projection. Raw responses belong to the device provider.
        fields = ("note_id", "author_id", "canonical_url", "title", "body", "media_type",
                  "published_at", "observed_at", "expected_pages")
        record = {key: data[key] for key in fields}
        record["metrics"] = {key: data["metrics"][key] for key in ("likes_raw", "saves_raw")}
        record["pages"] = [{key: p[key] for key in ("index", "file", "sha256")} for p in data["pages"]]
        pending = accepted.with_suffix(".pending")
        pending.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        pending.replace(accepted)
        return {"status": "captured", "expected_pages": data["expected_pages"], "visually_read_pages": 0}
    finally:
        lock.unlink(missing_ok=True)
