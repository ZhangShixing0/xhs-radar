"""Offline regressions; synthetic databases only, no browser or credentials."""
import hashlib
from contextlib import closing
import importlib.util
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('radar', ROOT / 'scripts/xhs-radar/xhs_radar.py')
radar = importlib.util.module_from_spec(spec)
spec.loader.exec_module(radar)
WRAPPER = ROOT / 'scripts/run-radar.ps1'


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8-sig')


class ReportTests(unittest.TestCase):
    def test_topic_gate_uses_three_distinct_authors_and_high_save_notes(self):
        items = [dict(topic_slug='magnet', topic_name='Magnet', author_id=f'a-{i}', collected_count=1500) for i in range(3)]
        items += [dict(topic_slug='magnet', author_id='a-0', collected_count=1800),
                  dict(topic_slug='low', author_id='a-4', collected_count=470)]
        result = radar.qualified_report_topics(items)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['distinct_author_count'], 3)
        self.assertEqual(result[0]['top_notes'][0]['collected'], 1800)
        self.assertEqual(radar.qualified_report_topics(items[:2]), [])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='radar-report-test-')
        self.root = Path(self.temp.name)
        self.db = self.root / 'synthetic.sqlite3'
        radar.initialize(self.db, ROOT / 'schemas/xhs-radar-v1.sql')
        self.run = self.root / 'offline-run'
        self.run.mkdir()
        plan = self.root / 'plan.json'
        write_json(plan, {'operations': [{'operation': i, 'layer': 'topic', 'query_text': f'query-{i}'} for i in range(1, 5)]})
        self.manifest = {'run_id': 'offline-run', 'status': 'completed', 'plan_path': str(plan),
                         'counts': {'operations_passed': 4},
                         'operation_results': [{'operation': i, 'status': 'passed'} for i in range(1, 5)]}
        write_json(self.run / 'manifest.json', self.manifest)
        rows = [{'operation': 1, 'feed_id': f'note-{i}', 'query_text': 'query-1', 'layer': 'topic', 'collected_count': 1500} for i in range(1, 4)]
        (self.run / 'observations.jsonl').write_text('\n'.join(json.dumps(r) for r in rows), encoding='utf-8')
        with sqlite3.connect(self.db) as conn:
            for run, date in [('old-topic', '2026-10-01'), ('offline-run-topic', '2026-10-02'), ('future-topic', '2026-10-03')]:
                conn.execute("INSERT INTO radar_runs(id,layer,status,started_at,config_hash,source_identity) VALUES (?,'topic','completed',?,'fake','synthetic')", (run, date))
            for i in range(1, 4):
                conn.execute("INSERT INTO notes(id,platform,platform_note_id,title,first_seen_at,last_seen_at,current_collected_count) VALUES (?,'xiaohongshu',?,'fixture','2026-10-01','2026-10-03',99999)", (i, f'note-{i}'))
                conn.execute("INSERT INTO note_snapshots(note_id,run_id,observed_at,liked_count,collected_count,comment_count,shared_count,source) VALUES (?,'offline-run-topic','2026-10-02T12:00:00+00:00',10,1500,0,0,?)", (i, 'detail' if i == 1 else 'search'))
            for i, run, date in [(1, 'old-topic', '2026-10-01'), (2, 'future-topic', '2026-10-03')]:
                conn.execute("INSERT INTO note_snapshots(note_id,run_id,observed_at,liked_count,collected_count,comment_count,shared_count,source) VALUES (?,?,?,0,1,0,0,'search')", (i, run, date))
            conn.execute("INSERT INTO note_details(note_id,captured_at,description) VALUES (1,'2026-10-02','fixture')")
            conn.execute("INSERT INTO topics(id,slug,name,created_at,updated_at) VALUES (1,'fixture','fixture-topic','2026-10-01','2026-10-01')")
            conn.execute("INSERT INTO note_topic_links(note_id,topic_id,confidence,assigned_by) VALUES (1,1,1,'manual_review')")
        conn.close()
        write_json(self.run / 'detail-selection-v2.json', {'selected': [{'feed_id': f'note-{i}'} for i in range(1, 4)]})
        (self.run / 'qualified-details-v2.jsonl').write_text('{"feed_id":"note-1"}\n', encoding='utf-8')
        write_json(self.run / 'qualified-details-v2-summary.json', {'status': 'partial'})

    def tearDown(self):
        self.temp.cleanup()

    def test_report_uses_actual_counts_full_history_and_current_run_metrics(self):
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        out = self.root / 'report.md'
        analysis = radar.write_collector_report(self.db, ROOT / 'config/keywords.example.json', self.run, out)['analysis']
        self.assertEqual(analysis['planned_operation_count'], 4)
        self.assertEqual(analysis['historical_overlap_count'], 1)
        self.assertEqual(analysis['new_note_count'], 2)  # Future observation is not historical overlap.
        self.assertEqual(analysis['baseline_overlap_count'], 0)
        self.assertEqual(analysis['detail_completed_count'], 1)
        self.assertEqual(analysis['detail_selected_count'], 3)
        self.assertEqual(analysis['detail_database_count'], 1)
        self.assertEqual(analysis['topic_summaries'][0]['median_collected'], 1500)
        text = out.read_text(encoding='utf-8-sig')
        self.assertIn('计划 4 个查询：完成 4', text)
        self.assertNotIn('20 个查询全部完成', text)
        self.assertNotIn('尚未补本轮详情', text)
        self.assertIn('未验证', text)
        self.assertEqual(before, hashlib.sha256(self.db.read_bytes()).hexdigest())

    def test_partial_and_failed_queries_are_not_reported_as_completed(self):
        self.manifest['status'] = 'partial'
        self.manifest['operation_results'] = [{'operation': 1, 'status': 'passed'}, {'operation': 2, 'status': 'failed'}]
        write_json(self.run / 'manifest.json', self.manifest)
        result = radar.analyze_collector_run(self.db, ROOT / 'config/keywords.example.json', self.run)
        self.assertEqual((result['operation_count'], result['failed_operation_count'], result['pending_operation_count']), (1, 1, 2))

    def test_unimported_run_keeps_local_observation_count(self):
        self.manifest['run_id'] = 'never-imported'
        self.manifest['operation_results'] = []
        self.manifest['status'] = 'failed'
        write_json(self.run / 'manifest.json', self.manifest)
        result = radar.analyze_collector_run(self.db, ROOT / 'config/keywords.example.json', self.run)
        self.assertEqual(result['unique_note_count'], 3)
        self.assertEqual(result['imported_note_count'], 0)


class SharedHistoryTests(unittest.TestCase):
    def test_repeated_imports_from_two_callers_share_note_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            device = Path(directory)
            db = device / "data/xhs-radar/xhs-radar.sqlite3"
            config = ROOT / "config/keywords.example.json"
            radar.initialize(db, ROOT / "schemas/xhs-radar-v1.sql")
            stamp = "2025-01-01T00:00:00Z"
            for name in ("muse-synthetic", "radar-synthetic"):
                run = db.parent / "runs" / name
                plan = device / (name + ".json")
                operation = {"query_key": name, "query_text": "fixture", "query_group": "exploration"}
                write_json(plan, {"operations": [operation]})
                manifest = {"run_id": name, "status": "completed", "plan_path": str(plan),
                            "plan_sha256": hashlib.sha256(plan.read_bytes()).hexdigest(),
                            "started_at": stamp, "completed_at": stamp, "operation_results": [],
                            "counts": {"operations_selected": 1, "operations_passed": 1, "observations": 1}}
                write_json(run / "manifest.json", manifest)
                row = dict(operation, run_id=name, feed_id="synthetic-note", author_id="synthetic-author",
                           observed_at=stamp, layer="recent", result_rank=1)
                (run / "observations.jsonl").write_text(json.dumps(row), encoding="utf-8")
                command = [sys.executable, "-B", str(ROOT / "scripts/xhs-radar/xhs_radar.py"),
                           "import-run", "--db", str(db), "--run-dir", str(run), "--config", str(config)]
                for attempt in range(2):
                    subprocess.run(command, check=True, stdout=subprocess.DEVNULL)
            with closing(sqlite3.connect(db)) as conn:
                self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0])
                self.assertEqual(2, conn.execute("SELECT COUNT(*) FROM note_snapshots").fetchone()[0])
                sources = {row[0] for row in conn.execute("SELECT source_identity FROM radar_runs")}
                self.assertEqual({f"data/xhs-radar/runs/{name}" for name in ("muse-synthetic", "radar-synthetic")}, sources)


@unittest.skipUnless(shutil.which("powershell.exe"), "requires Windows PowerShell")
class WrapperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='radar-wrapper-test-')
        self.root = Path(self.temp.name)
        self.scripts = self.root / 'scripts/xhs-radar'
        self.scripts.mkdir(parents=True)
        shutil.copyfile(WRAPPER, self.root / "scripts/run-radar.ps1")
        shutil.copyfile(ROOT / "scripts/xhs-radar/readonly-provider-version.ps1", self.scripts / "readonly-provider-version.ps1")
        self.device = self.root / "device"
        self.run = self.device / 'data/xhs-radar/runs/offline-fixture-01'
        self.run.mkdir(parents=True)
        (self.device / "data/xhs-radar/xhs-radar.sqlite3").touch()
        write_json(self.device / 'config/xhs-radar-keywords.json', {})
        write_json(self.run / 'manifest.json', {'run_id': 'offline-fixture-01', 'status': 'completed', 'counts': {}})
        self.selection = self.run / 'detail-selection-v2.json'
        write_json(self.selection, {'selected': [{'feed_id': 'fixture'}]})
        (self.scripts / 'collect-radar-run.ps1').write_text("throw 'Unexpected search'", encoding='utf-8-sig')
        (self.scripts / 'build-detail-candidates.py').write_text("raise RuntimeError('Selection must not be rebuilt')", encoding='utf-8')
        (self.scripts / 'xhs_radar.py').write_text(
            "import sys\nfrom pathlib import Path\n"
            "root=Path(__file__).resolve().parents[2]\n"
            "with (root/'commands.txt').open('a') as f: f.write(sys.argv[1]+'\\n')\n"
            "if sys.argv[1]=='report-run': Path(sys.argv[sys.argv.index('--report')+1]).write_text('synthetic report')\n",
            encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def collector(self, status):
        code = r"""param($RunId,$BatchSize,$DelaySeconds,[switch]$Resume)
$root = $env:XHS_RADAR_ENGINE_ROOT
$run = Join-Path $root "data\xhs-radar\runs\$RunId"
if (-not $Resume) { throw 'Expected explicit resume forwarding' }
@{ status='STATUS'; stop_reason='fixture'; counts=@{selected=3;completed=1} } |
    ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 -LiteralPath (Join-Path $run 'qualified-details-v2-summary.json')
if ('STATUS' -eq 'failed') { exit 2 }
"""
        (self.scripts / 'collect-qualified-details.ps1').write_text(code.replace('STATUS', status), encoding='utf-8-sig')

    def invoke(self, resume=True):
        args = ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(self.root / "scripts/run-radar.ps1"), '-Action', 'details',
                '-RunId', 'offline-fixture-01', '-EngineRoot', str(self.device), '-PythonPath', sys.executable]
        if resume:
            args.append('-ResumeDetails')
        return subprocess.run(args, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)

    def test_partial_batch_retains_selection_and_does_not_import(self):
        self.collector('partial')
        before = self.selection.read_bytes()
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'partial')
        self.assertEqual((self.root / 'commands.txt').read_text().splitlines(), ['report-run'])
        self.assertEqual(before, self.selection.read_bytes())

    def test_completed_batch_imports_and_reports(self):
        self.collector('completed')
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['detail_status'], 'completed')
        self.assertEqual((self.root / 'commands.txt').read_text().splitlines(), ['import-details', 'report-run'])

    def test_failed_batch_keeps_failure_and_reports_without_import(self):
        self.collector('failed')
        result = self.invoke()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'failed')
        self.assertEqual((self.root / 'commands.txt').read_text().splitlines(), ['report-run'])

    def test_existing_output_requires_explicit_resume_before_any_action(self):
        self.collector('partial')
        write_json(self.run / 'qualified-details-v2-summary.json', {'status': 'partial'})
        result = self.invoke(resume=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('ResumeDetails', result.stderr)
        self.assertFalse((self.root / 'commands.txt').exists())

    def test_resume_missing_selection_refuses_to_regenerate(self):
        self.collector('partial')
        self.selection.unlink()
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Original detail selection is missing', result.stderr)
        self.assertFalse((self.root / 'commands.txt').exists())


if __name__ == '__main__':
    unittest.main()
