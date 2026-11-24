"""Offline integration fixture. Run with python -B radar/test_adapter.py."""
import base64
from contextlib import ExitStack, nullcontext
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from provider import CAPABILITIES
from native_batch import native_originals, selection
from owned_collector import HELPERS, private_collector, private_wrapper
from radar import PACKAGED_WRAPPER, REVIEWED_WRAPPER
from radar import run_radar
from seat import windows_environment


HERE = Path(__file__).resolve().parent


def fixture():
    request = json.load(sys.stdin)
    answer = {"protocol": "s2-original/1", "network_request_made": False}
    if request["action"] == "capabilities":
        answer["capabilities"] = dict.fromkeys(CAPABILITIES, True)
    else:
        root = Path(request["media_root"])
        answer.update({key: request[key] for key in ("note_id", "author_id", "canonical_url")})
        answer.update(status="captured", media_type="photo", title="fixture", body="",
                      published_at="2025-01-01T00:00:00Z", observed_at="2025-01-02T00:00:00Z",
                      metrics={"likes_raw": "0", "saves_raw": None}, expected_pages=1,
                      pages=[{"index": 1, "file": "synthetic.png",
                              "sha256": hashlib.sha256((root / "synthetic.png").read_bytes()).hexdigest()}])
    print(json.dumps(answer))


class AdapterTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "requires Windows process handles")
    def test_owned_process_only_and_busy_cleanup_is_empty(self):
        code = HELPERS + r"""
$ErrorActionPreference = 'Stop'
$exe = (Get-Process -Id $PID).Path
$argv = @('-NoProfile', '-NonInteractive', '-Command', 'Start-Sleep -Seconds 45')
$foreign = $null
$owned = $null
try {
    $foreign = Start-Process $exe -ArgumentList $argv -WindowStyle Hidden -PassThru
    Stop-S2OwnedProvider $null
    Stop-S2OwnedProvider $foreign
    if ($foreign.HasExited) { throw 'A busy-seat cleanup stopped another process' }
    $owned = Start-Process $exe -ArgumentList $argv -WindowStyle Hidden -PassThru
    Register-S2OwnedProvider $owned
    Stop-S2OwnedProvider $foreign
    Stop-S2OwnedProvider $null
    if ($owned.HasExited -or $foreign.HasExited) { throw 'Non-owner cleanup acted' }
    Stop-S2OwnedProvider $owned
    if (-not $owned.HasExited -or $foreign.HasExited) { throw 'Owner cleanup failed' }
} finally {
    foreach ($p in @($owned, $foreign)) {
        if ($null -ne $p -and -not $p.HasExited) { $p.Kill() }
    }
}
"""
        command = base64.b64encode(code.encode("utf-16le")).decode()
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", command],
                                env=windows_environment(), capture_output=True, timeout=35)
        self.assertEqual(0, result.returncode, result.stderr)

    @unittest.skipUnless(os.name == "nt", "requires installed Windows native collectors")
    def test_old_prepared_run_and_default_collectors_parse_without_touching_source(self):
        # Optional integration fixture on installed shared hosts. All output goes to a temporary directory.
        if not os.environ.get("S2_RESEARCH_ROOT"):
            self.skipTest("No historical S2 receipt root configured")
        root = Path(os.environ["S2_RESEARCH_ROOT"])
        receipts = list((root / ".runtime/s2").glob("muse-*/*/adapter-receipt.json"))
        if not receipts:
            self.skipTest("No installed fixed-ID batch to replay")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            receipt = receipts[0]
            binding = json.loads(receipt.read_text(encoding="utf-8-sig"))
            source = receipt.parent / "collect-s2-batch.ps1"
            before = source.read_bytes()
            native = private_collector(source, binding["adapter_sha256"], out, "native")
            self.assertEqual(native, private_collector(source, binding["adapter_sha256"], out, "native"))
            self.assertEqual(source.read_bytes(), before)
            self.assertNotEqual(source, native)
            content = native.read_text(encoding="utf-8-sig")
            self.assertIn("shared_seat_busy", content)
            self.assertNotIn("Stop-IsolatedBrowserProcesses", content)
            wrapper = PACKAGED_WRAPPER
            for mode in ("search", "details"):
                derived = private_wrapper(wrapper, REVIEWED_WRAPPER, HERE.parent, out, mode)
                self.assertIn("owner_bound_digest_changed", derived.read_text(encoding="utf-8-sig"))
            parse = ("$ErrorActionPreference='Stop'; foreach ($f in (Get-ChildItem -LiteralPath $env:S2_PARSE_DIR))"
                     " { $t=$null; $e=$null; [void][Management.Automation.Language.Parser]::"
                     "ParseFile($f.FullName,[ref]$t,[ref]$e); if (@($e).Count){throw ($e | Out-String)} }")
            result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", parse],
                                    env=windows_environment(dict(os.environ, S2_PARSE_DIR=str(out))),
                                    capture_output=True, timeout=20)
            self.assertEqual(0, result.returncode, result.stderr)

    def test_native_selection_is_same_run_and_same_author(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            run = "muse-fixture-native"
            batch = "pet-demo"
            first, second = "1" * 24, "2" * 24
            source = base / "data" / "xhs-radar" / "runs" / run
            source.mkdir(parents=True)
            private = base / "private"
            (private / "fixed-queue").mkdir(parents=True)
            plan = private / "plan.json"
            plan.write_bytes(b"{}")
            manifest = {"run_id": run, "status": "completed",
                        "plan_sha256": hashlib.sha256(plan.read_bytes()).hexdigest()}
            (source / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            rows = []
            for nid, role in [(first, "high_candidate"), (second, "boundary")]:
                url = "https://www.xiaohongshu.com/explore/" + nid
                task = {"run_id": run, "note_id": nid, "author_id": "fixture", "canonical_url": url,
                        "planned_role": role, "reason": "sample"}
                rows.append({"feed_id": nid, "run_id": run, "author_id": "fixture", "canonical_url": url})
                (private / "fixed-queue" / (nid + ".json")).write_text(json.dumps(task), encoding="utf-8")
            ledger = source / "observations.jsonl"
            ledger.write_text("\n".join(map(json.dumps, rows)), encoding="utf-8")
            chosen = selection(run, batch, plan, private, source, [first, second])
            self.assertEqual(["high_engagement_candidate", "control_candidate"],
                             [row["sample_role"] for row in chosen["selected"]])
            self.assertEqual(hashlib.sha256(ledger.read_bytes()).hexdigest(), chosen["source_observations_sha256"])
            rows[0]["author_id"] = "conflicting"
            ledger.write_text("\n".join(map(json.dumps, rows)), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Original author"):
                selection(run, batch, plan, private, source, [first])

    def test_busy_seat_skips_collector(self):
        args = SimpleNamespace(action="search", run="muse-fixture-native", delay=90)
        with ExitStack() as mocks:
            mocks.enter_context(patch("radar.load_plan", return_value=Path("fixture.json")))
            mocks.enter_context(patch("radar.wrapper_command", return_value=["powershell.exe"]))
            mocks.enter_context(patch("radar.reserve", side_effect=ValueError("shared_seat_busy")))
            spawn = mocks.enter_context(patch("radar.subprocess.run"))
            with self.assertRaisesRegex(ValueError, "shared_seat_busy"):
                run_radar(args)
            spawn.assert_not_called()

    def test_prepared_checkpoint_reuses_original_queue_with_new_private_collector(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            run, batch = "muse-fixture-resume", "detail-fixture"
            dest = base / ".runtime/s2" / run / batch
            dest.mkdir(parents=True)
            private = base / "private"
            private.mkdir()
            spec = {"selected": [{"feed_id": "0" * 24}]}
            digest = hashlib.sha256(b"fixture").hexdigest()
            (dest / "collect-s2-batch.ps1").write_bytes(b"fixture")
            (dest / "detail-selection-v2.json").write_bytes(b"fixture")
            (dest / "selection-request.json").write_text(json.dumps(spec), encoding="utf-8")
            receipt = {"run_id": run, "batch_id": batch, "selection_sha256": digest, "adapter_sha256": digest}
            (dest / "adapter-receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
            state = dest / "qualified-details-v2-summary.json"
            state.write_bytes(b'{"status":"partial"}')
            guarded = private / "owner-bound" / "native-reviewed.ps1"
            args = SimpleNamespace(run=run, batch=batch, note=["0" * 24], execute=True, resume=True,
                                   source_run=str(base), delay=90)
            with ExitStack() as mocks:
                mocks.enter_context(patch.dict(os.environ, S2_RESEARCH_ROOT=str(base)))
                mocks.enter_context(patch("native_batch.selection", return_value=spec))
                mocks.enter_context(patch("native_batch.reserve", return_value=nullcontext()))
                guard = mocks.enter_context(patch("native_batch.private_collector", return_value=guarded))
                spawn = mocks.enter_context(patch("native_batch.subprocess.run", return_value=SimpleNamespace(returncode=0)))
                result = native_originals(args, private / "plan.json", private, "powershell.exe")
                guard.assert_called_once_with(dest / "collect-s2-batch.ps1", digest, guarded.parent, "native")
                command = spawn.call_args.args[0]
                self.assertEqual(str(guarded), command[command.index("-File") + 1])
                self.assertIn("-Resume", command)
                self.assertEqual("partial", result["stage"])
                self.assertEqual(b'{"status":"partial"}', state.read_bytes())

    def test_native_system_modules_have_priority_only_in_child_process(self):
        old = {"SYSTEMROOT": "C:/Windows", "PSMODULEPATH": "custom;inherited"}
        new = windows_environment(old)
        paths = new["PSMODULEPATH"].split(";")
        self.assertTrue(Path(paths[0]).as_posix().endswith("System32/WindowsPowerShell/v1.0/Modules"))
        self.assertEqual(["custom", "inherited"], paths[1:])
        self.assertEqual("custom;inherited", old["PSMODULEPATH"])

    def test_local_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            media = base / "media"
            media.mkdir()
            # This fixture tests transport and hashes only. No image interpretation is performed.
            pixel = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
            (media / "synthetic.png").write_bytes(base64.b64decode(pixel))
            state = base / "state"
            env = dict(os.environ, S2_MUSE_RADAR_STATE=str(state), S2_MUSE_RADAR_MEDIA_ROOT=str(media),
                       S2_MUSE_ORIGINAL_PROVIDER=json.dumps([sys.executable, "-B", str(Path(__file__).resolve()),
                                                            "--fixture"]))
            def cli(*args, expected=0):
                p = subprocess.run([sys.executable, "-B", str(HERE / "radar.py"), *args], env=env,
                                   capture_output=True)
                self.assertEqual(expected, p.returncode, p.stderr)
                return json.loads(p.stdout) if not expected else None
            run = "muse-synthetic-test"
            cli("plan", "--run", run, "--keyword", "fixture")
            cli("plan", "--run", run, "--keyword", "different", expected=1)
            cli("inspect", "--run", "old-case", expected=1)
            self.assertEqual("fixture", cli("inspect", "--run", run)["operations"][0]["query_text"])
            public = base / "device" / "data/xhs-radar/runs" / run
            public.mkdir(parents=True)
            plan = next((state / run).glob("plan-*.json"))
            manifest = {"status": "completed", "run_id": run, "plan_sha256": hashlib.sha256(plan.read_bytes()).hexdigest()}
            (public / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            note = "0" * 24
            row = {"feed_id": note, "run_id": run, "author_id": "fixture", "title": "fixture",
                   "observed_at": "2025-01-01T00:00:00Z", "liked_count_raw": "5", "collected_count_raw": "2",
                   "canonical_url": "https://www.xiaohongshu.com/explore/" + note}
            (public / "observations.jsonl").write_text(json.dumps(row), encoding="utf-8")
            cli("queue", "--run", run, "--source-run", str(public), "--note", note,
                "--role", "ordinary", "--reason", "synthetic test")
            verify_queue = ("check-queue", "--run", run, "--source-run", str(public))
            self.assertEqual(1, cli(*verify_queue)["entries"])
            native = ("native-originals", "--run", run, "--source-run", str(public),
                      "--batch", "fixture-batch", "--note", note)
            self.assertEqual("prepared", cli(*native)["stage"])
            self.assertFalse(cli(*native)["network_request_made"])
            ledger = public / "observations.jsonl"
            original = ledger.read_bytes()
            ledger.write_text(json.dumps(dict(row, liked_count_raw="6")), encoding="utf-8")
            cli(*verify_queue, expected=1)
            ledger.write_bytes(original)
            self.assertFalse(cli("provider-status")["network_request_made"])
            command = ["read-original", "--run", run, "--note", note]
            self.assertEqual(0, cli(*command)["visually_read_pages"])
            del env["S2_MUSE_ORIGINAL_PROVIDER"]
            self.assertEqual("reused", cli(*command)["status"])
            (media / "synthetic.png").write_bytes(b"changed")
            cli(*command, expected=1)
            cli("provider-status", expected=1)


if __name__ == "__main__":
    if sys.argv[1:] == ["--fixture"]:
        fixture()
    else:
        unittest.main()
