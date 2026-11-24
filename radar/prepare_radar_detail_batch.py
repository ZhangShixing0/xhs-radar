"""Prepare a run-bound, independent detail batch; never start collection or log in.

Reads the authorized run's public manifest/observations and reviewed collector
code. Its generated adapter lets the original central collector own the private
lookup internally. Existing central detail outputs are neither read nor changed.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory

from research_selection import instant, select_notes

# Exact binding digests accepted by the original device-side release checks.
REVIEWED_BINDINGS = {
    '9b49a0215e91b80d6089dacb6d65069769fefd3e7688e72df98bfb194f3e6a90',
    'ad8cc12f404172d277f0b2eea245b253800cd021e8b909083a087b42316946eb',
    '5458a3d7141b76ae6e9ccad45792eca241a51726b200364d7b8f8be680dd0d46',
}
COLLECTOR_SHA256 = '4b5ec7ee73fe2357d478cd0de03c79723e3ff1f76f89d9a30bb48935c32a3ee4'
LEGACY_COLLECTOR_SHA256 = '81ffb4c1c26d9ed31464b358a2c9373a2a940cba27ced12a7521d1ec0e8ad170'
BINDING_COLLECTOR_SHA256 = '8fa489e86b4fec68da4782394141952ad94f013f47288d1d69cc31e2ded34507'
LEGACY_COMMON_SHA256 = 'e7fbdffec29f9564f552d160d70c364015f3528778cc136195a664a2898d3776'
CLASSIFIER_SHA256 = '1a1e88d4148e2723cd6bf6f2595043a5a17cc8a090b5693faa59bc68f1a4f4ec'
CLASSIFIER = Path(__file__).with_name('session-lifecycle.candidate.ps1')
RUN_PATTERN = re.compile(r'[a-z0-9][a-z0-9-]{5,80}')
NOTE_PATTERN = re.compile(r'[a-f0-9]{24}')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def exact(value):
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value)
    return None


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError('Reviewed collector anchor changed; refusing adaptation')
    return text.replace(old, new, 1)


def add_execution_diagnostics(text):
    """Observe the tool dispatch boundary; never infer calls from failure labels."""
    helper = r'''function Get-S2DetailFailureKind {
    param([AllowEmptyString()][string]$Message, [string]$Stage)
    # Exact provider messages reviewed in feed_detail.go / collector source.
    # Return only a fixed enum, never the input message or a matched substring.
    if ($Message -match [regex]::Escape('无法获取初始状态数据')) { return 'initial_state_unavailable' }
    if ($Message -match [regex]::Escape('笔记不可访问:')) { return 'note_unavailable' }
    if ($Message -match 'feed [a-f0-9]{24} not found in noteDetailMap') { return 'note_identity_missing' }
    if ($Message -match [regex]::Escape('failed to unmarshal noteDetailMap:')) { return 'provider_parse_error' }
    if ($Message -match [regex]::Escape('Detail response did not contain a note.')) { return 'local_parse_error' }
    if ($Message -match [regex]::Escape('MCP response was neither JSON nor an SSE data event.')) { return 'local_parse_error' }
    if ($Stage -in @('mcp_response_parse', 'local_field_parse')) { return 'local_parse_error' }
    if ($Message -match [regex]::Escape('context deadline exceeded')) {
        if ($Stage -eq 'detail_request') { return 'transport_timeout' }
        if ($Stage -eq 'tool_response_check') { return 'provider_timeout' }
    }
    return 'unknown'
}

'''
    text = replace_once(text, 'function Invoke-ReadOnlyTool {', helper + 'function Invoke-ReadOnlyTool {')
    text = replace_once(text, "    $http = Invoke-McpPost -Payload @{", """    if ($Name -eq 'get_feed_detail') {
        # Set only at the actual permitted tool request boundary, after preflight.
        $script:S2DetailDiagnostic.detail_tool_called = $true
        $script:S2DetailDiagnostic.stage = 'detail_request'
    }
    $http = Invoke-McpPost -Payload @{""")
    text = replace_once(text, '    $result = ConvertFrom-McpHttpResponse -Response $http', """    if ($Name -eq 'get_feed_detail') { $script:S2DetailDiagnostic.stage = 'mcp_response_parse' }
    $result = ConvertFrom-McpHttpResponse -Response $http
    if ($Name -eq 'get_feed_detail') { $script:S2DetailDiagnostic.stage = 'tool_response_check' }""")
    text = replace_once(text, "    $errorCategory = ''\n    $session = $null", """    $errorCategory = ''
    $script:S2DetailDiagnostic = @{ stage = 'preflight'; detail_tool_called = $false; failure_kind = 'none' }
    $session = $null""")
    text = replace_once(text, '        $data = ConvertFrom-CaseSensitiveJson `', """        $script:S2DetailDiagnostic.stage = 'local_field_parse'
        $data = ConvertFrom-CaseSensitiveJson `""")
    text = replace_once(text, '        $succeeded++', """        $succeeded++
        $script:S2DetailDiagnostic.stage = 'complete'""")
    text = replace_once(text, '        $message = [string]$_.Exception.Message', """        $message = [string]$_.Exception.Message
        $script:S2DetailDiagnostic.failure_kind = Get-S2DetailFailureKind -Message $message -Stage $script:S2DetailDiagnostic.stage""")
    text = replace_once(text, '        error_category = $errorCategory', """        error_category = $errorCategory
        execution_stage = [string]$script:S2DetailDiagnostic.stage
        detail_tool_called = [bool]$script:S2DetailDiagnostic.detail_tool_called
        failure_kind = [string]$script:S2DetailDiagnostic.failure_kind""")
    return text


def ps_literal(value):
    text = str(value)
    if any(c in text for c in ('\r', '\n', '\x00')):
        raise ValueError('Invalid path or identifier')
    return "'" + text.replace("'", "''") + "'"


def verify_selection_sources(spec, manifest_path, manifest_raw, observations_path, observations_raw):
    expected = {str(manifest_path): sha(manifest_raw), str(observations_path): sha(observations_raw)}
    direct = (spec.get('source_manifest_sha256') == sha(manifest_raw) and
              spec.get('source_observations_sha256') == sha(observations_raw))
    if not direct:
        files = spec.get('source_files')
        if not isinstance(files, list):
            raise ValueError('Selection must bind the exact public manifest and observations hashes')
        seen = {}
        for item in files:
            if not isinstance(item, dict) or not isinstance(item.get('path'), str):
                raise ValueError('Invalid selection source reference')
            path = str(Path(item['path']).resolve())
            if path in seen:
                raise ValueError('Duplicate selection source reference')
            seen[path] = item.get('sha256')
        if any(seen.get(path) != digest for path, digest in expected.items()):
            raise ValueError('Selection sources do not match this run')


def make_selection(spec, rows, manifest, *, run_id, batch_id, as_of):
    if spec.get('collector_run_id') != run_id or manifest.get('run_id') != run_id:
        raise ValueError('Selection or manifest belongs to another run')
    if manifest.get('status') != 'completed':
        raise ValueError('Only a completed public search run can supply details')
    requested = spec.get('selected')
    if not isinstance(requested, list) or not 1 <= len(requested) <= 24:
        raise ValueError('Each prepared batch must select 1 to 24 notes')
    instant(as_of)
    notes = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or row.get('run_id') != run_id:
            raise ValueError('Observation does not belong to this run')
        nid = row.get('feed_id')
        url = row.get('canonical_url')
        if (not isinstance(nid, str) or not NOTE_PATTERN.fullmatch(nid) or
                url != 'https://www.xiaohongshu.com/explore/' + nid):
            raise ValueError('Observation identity URL is invalid')
        notes.append({'id': nid, 'url': url, 'author_id': row.get('author_id'),
                      'title': row.get('title', ''), 'body': '', 'published_at': None,
                      'collected_at': row.get('observed_at'),
                      'likes': exact(row.get('liked_count_raw')),
                      'saves': exact(row.get('collected_count_raw')),
                      'raw_metrics': {'likes': row.get('liked_count_raw'), 'saves': row.get('collected_count_raw')},
                      'provenance': {'observation_index': index}})
    # Zero here only admits exact low-interaction controls into acquisition.
    # High-interaction role is separately checked against the unchanged 100/20 gate.
    selection = select_notes(notes, {'since': '1970-01-01T00:00:00+00:00', 'as_of': as_of,
                                    'include_undated': True, 'min_likes': 0, 'min_saves': 0,
                                    'threshold_mode': 'any', 'min_authors': 1})
    available = {note['id']: note for note in selection['notes']}
    selected, seen = [], set()
    for request in requested:
        if not isinstance(request, dict):
            raise ValueError('Invalid requested candidate')
        nid = request.get('feed_id')
        if not isinstance(nid, str) or nid in seen or nid not in available:
            raise ValueError('Candidate is duplicated, absent, conflicting, or lacks exact public metrics')
        seen.add(nid)
        note = available[nid]
        index = note['provenance']['observation_index']
        if 'observation_index' in request and request['observation_index'] != index:
            raise ValueError('Requested observation does not match the current selected snapshot')
        role = request.get('sample_role')
        if role is None and request.get('qualification_tier') == 's2_engagement_candidate_material_unverified':
            role = 'high_engagement_candidate'
        if role not in ('control_candidate', 'high_engagement_candidate'):
            raise ValueError('Each candidate needs an explicit control or high-engagement acquisition role')
        meets_gate = note['likes'] >= 100 or note['saves'] >= 20
        if role == 'high_engagement_candidate' and not meets_gate:
            raise ValueError('High-engagement candidate does not meet 100 likes OR 20 saves')
        reason = request.get('selection_reason')
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError('Each candidate needs a reviewable selection reason')
        row = rows[index]
        selected.append({
            'selection_rank': len(selected)+1, 'feed_id': nid, 'canonical_url': note['url'],
            'title': note['title'], 'author_id': note['author_id'], 'author': row.get('author', ''),
            'liked_count': note['likes'], 'collected_count': note['saves'],
            'liked_count_raw': row['liked_count_raw'], 'collected_count_raw': row['collected_count_raw'],
            'observed_at': note['collected_at'], 'observation_index': index,
            'source_queries': sorted({r['query_text'] for r in rows if r.get('feed_id') == nid and isinstance(r.get('query_text'), str)}),
            'sample_role': role, 'qualification_tier': role,
            'selection_reason': reason, 'meets_interaction_gate': meets_gate,
            'topic_slug': str(request.get('topic_slug', '')), 'topic_name': str(request.get('topic_name', '')),
            'material_verified': False, 'body_read': False, 'media_reviewed': False,
        })
    return {'schema_version': '2.0', 'collector_run_id': run_id, 'batch_id': batch_id,
            'purpose': 'detail_acquisition_candidates_including_controls_not_research_conclusions',
            'gate': {'min_collected': 0, 'target': len(selected), 'direct_math_title_required': False,
                     'max_per_author': max(Counter(n['author_id'] for n in selected).values()),
                     'compatibility_note': 'Legacy acquisition floor only; control_candidate is never certified high interaction.',
                     's2_policy': {'min_likes': 100, 'min_saves': 20, 'threshold_mode': 'any', 'as_of': as_of}},
            'selected_count': len(selected), 'selected': selected,
            'quality_flags': selection['quality_flags'], 'rejected_observations': selection['rejected'],
            'network_request_made': False, 'research_qualified_count': None}


def prepare_batch(*, selection_path, run_id, batch_id, output, engine_root, private_root, as_of=None):
    if not RUN_PATTERN.fullmatch(run_id) or not RUN_PATTERN.fullmatch(batch_id):
        raise ValueError('Run and batch IDs must be lowercase letters/digits/hyphens, 6 to 81 characters')
    target = Path(output).resolve()
    allowed = Path(private_root).resolve()
    if allowed.is_relative_to(Path(__file__).resolve().parents[1]):
        raise ValueError('Keep prepared batches outside the repository')
    if target != allowed / run_id / batch_id or target.exists():
        raise ValueError('Output must be a new private directory bound to this run and batch')
    engine = Path(engine_root).resolve()
    run_root = engine / 'data/xhs-radar/runs' / run_id
    manifest_path, observations_path = run_root/'manifest.json', run_root/'observations.jsonl'
    code_root = Path(__file__).resolve().parents[1]
    collector_path = code_root / 'scripts/xhs-radar/collect-qualified-details.ps1'
    collector_raw = collector_path.read_bytes()
    collector_sha = sha(collector_raw)
    if collector_sha not in (COLLECTOR_SHA256, BINDING_COLLECTOR_SHA256, LEGACY_COLLECTOR_SHA256):
        raise ValueError('Central detail collector changed; review its new version before preparing')
    legacy = collector_sha == LEGACY_COLLECTOR_SHA256
    text = collector_raw.decode('utf-8-sig').replace('\r\n', '\n')
    dependencies = []
    binding_sha = None
    if not legacy:
        binding = code_root / 'scripts/xhs-radar/readonly-provider-version.ps1'
        if not binding.is_file() or not binding.resolve().is_relative_to(code_root):
            raise ValueError('Selected engine has no local reviewed provider binding')
        binding_sha = sha(binding.read_bytes())
        if binding_sha not in REVIEWED_BINDINGS:
            raise ValueError('Selected engine provider binding is not a reviewed release')
        dependencies.append((binding, binding_sha))
        text = replace_once(text, ". (Join-Path $PSScriptRoot 'readonly-provider-version.ps1')", '. ' + ps_literal(binding))
    classifier_raw = CLASSIFIER.read_bytes()
    if sha(classifier_raw) != CLASSIFIER_SHA256:
        raise ValueError('Reviewed status classifier changed')
    manifest_raw, observations_raw = manifest_path.read_bytes(), observations_path.read_bytes()
    spec_path = Path(selection_path).resolve()
    spec_raw = spec_path.read_bytes()
    spec = json.loads(spec_raw.decode('utf-8-sig'))
    verify_selection_sources(spec, manifest_path, manifest_raw, observations_path, observations_raw)
    manifest = json.loads(manifest_raw.decode('utf-8-sig'))
    rows = [json.loads(line) for line in observations_raw.decode('utf-8-sig').splitlines() if line.strip()]
    selected = make_selection(spec, rows, manifest, run_id=run_id, batch_id=batch_id,
                              as_of=as_of or datetime.now(timezone.utc).isoformat())
    selected['source_files'] = [{'path': str(path), 'sha256': sha(raw)} for path,raw in
                                [(manifest_path,manifest_raw),(observations_path,observations_raw),(spec_path,spec_raw)]]
    selection_raw = (json.dumps(selected, ensure_ascii=False, indent=2)+'\n').encode('utf-8')
    for name in ('common.ps1', 'port-guard.ps1', 'case-sensitive-json.ps1'):
        path = code_root / 'scripts/xhs-local' / name
        dep_sha = sha(path.read_bytes())
        if legacy and name == 'common.ps1' and dep_sha != LEGACY_COMMON_SHA256:
            raise ValueError('Legacy collector requires its reviewed engine-local provider binding in common.ps1')
        dependencies.append((path, dep_sha))
        text = replace_once(text, ". (Join-Path $PSScriptRoot '..\\xhs-local\\"+name+"')", '. '+ps_literal(path))
    classifier_target = target/'session-lifecycle.ps1'
    text = replace_once(text, ". (Join-Path $PSScriptRoot 'session-lifecycle.ps1')", '. '+ps_literal(classifier_target))
    for variable, name in [('SelectionPath','detail-selection-v2.json'), ('DetailsJsonl','qualified-details-v2.jsonl'),
                           ('DetailsCsv','qualified-details-v2.csv'), ('SummaryPath','qualified-details-v2-summary.json')]:
        text = replace_once(text, f"${variable} = Join-Path $RunRoot '{name}'", f'${variable} = '+ps_literal(target/name))
    anchor = "            image_count = $images.Count"
    raw_assignments = []
    for name, key in [('liked_count', 'likedCount'), ('collected_count', 'collectedCount'),
                      ('comment_count', 'commentCount'), ('shared_count', 'sharedCount')]:
        field = f"$interact['{key}']"
        prefix = f"            {name}_raw = "
        if collector_sha != COLLECTOR_SHA256:
            raw_assignments.append(prefix + field)
        else:
            text = replace_once(text, prefix + '[string]' + field + '\n', prefix + field + '\n')
    if raw_assignments:
        text = replace_once(text, anchor, '\n'.join(raw_assignments + [anchor]))
        # Prior collectors predate the centrally enforced note/author pair check.
        marker = "        if (-not $note) { throw 'Detail response did not contain a note.' }"
        text = replace_once(text, marker, marker + """
        if ([string]$note['noteId'] -cne $feedId) { throw 'detail_note_identity_mismatch' }
        if ([string]::IsNullOrWhiteSpace([string]$candidate['author_id'])) { throw 'detail_expected_author_unknown' }
        if ($null -eq $note['user'] -or [string]$note['user']['userId'] -cne [string]$candidate['author_id']) {
            throw 'detail_author_identity_mismatch'
        }""")
    text = replace_once(text, "        $interact = $note['interactInfo']",
                        "        $interact = $note['interactInfo']\n        if ($null -eq $interact) { $interact = @{} }")
    text = replace_once(text, '        run_id = $RunId', '        run_id = $RunId\n        batch_id = '+ps_literal(batch_id))
    old_error_record = '''        $null = $errors.Add([ordered]@{
            feed_id = $feedId
            category = $errorCategory
        })'''
    public_error_record = '''        $publicError = [ordered]@{
            feed_id = $feedId
            category = $errorCategory
        }
        # Preserve only the pinned classifier's sanitized status contract.
        # Never store $message, the exception, raw tool responses or URL fields.
        if ($disposition.PSObject.Properties.Name -contains 'StructuredCategory') {
            $publicError.structured_category = $disposition.StructuredCategory
        }
        if ($disposition.PSObject.Properties.Name -contains 'SiteCheckDiagnostics') {
            $publicError.site_check_diagnostics = @(
                foreach ($check in $disposition.SiteCheckDiagnostics) {
                    [ordered]@{
                        envelope_index = $check.envelope_index
                        parse_state = $check.parse_state
                        status = $check.status
                        is_logged_in = $check.is_logged_in
                        selected_site = $check.selected_site
                        checked_at = $check.checked_at
                        sites = @(
                            foreach ($site in $check.sites) {
                                [ordered]@{
                                    site = $site.site
                                    status = $site.status
                                    reason = $site.reason
                                    checked_at = $site.checked_at
                                }
                            }
                        )
                    }
                }
            )
        }
        $null = $errors.Add($publicError)'''
    text = replace_once(text, old_error_record, public_error_record)
    text = add_execution_diagnostics(text)
    if '$PSScriptRoot' in text:
        raise ValueError('Unreviewed relative dependency remains in collector')
    # All guards run before the central common script or runtime initialization.
    checks = [(collector_path,collector_sha), *dependencies, (classifier_target,CLASSIFIER_SHA256),
              (target/'detail-selection-v2.json',sha(selection_raw)), (manifest_path,sha(manifest_raw)),
              (observations_path,sha(observations_raw))]
    guard = "\nif ($RunId -cne "+ps_literal(run_id)+") { throw 'Adapter is bound to another run' }\n"
    guard += "$env:XHS_RADAR_ENGINE_ROOT = " + ps_literal(engine) + "\n"
    guard += "if ($BatchSize -lt 0 -or $BatchSize -gt "+str(len(selected['selected']))+") { throw 'BatchSize exceeds prepared selection' }\n"
    for path, digest in checks:
        guard += "if ((Get-FileHash -LiteralPath "+ps_literal(path)+" -Algorithm SHA256).Hash -ine "+ps_literal(digest)+") { throw 'Prepared dependency or public selection changed; regenerate after review' }\n"
    text = replace_once(text, "$ErrorActionPreference = 'Stop'", "$ErrorActionPreference = 'Stop'\n"+guard)
    # Central original outputs are never touched; original private lookup is not
    # read by this preparer, and remains handled only by the collector at execution.
    if text.count('$LookupPath = Join-Path $RuntimeRoot') != 1:
        raise ValueError('Private lookup routing anchor changed')
    collector_bytes = text.replace('\n', '\r\n').encode('utf-8-sig')
    receipt = {'kind':'prepared_independent_radar_detail_batch', 'run_id':run_id, 'batch_id':batch_id,
               'selected_count':len(selected['selected']), 'roles':dict(Counter(n['sample_role'] for n in selected['selected'])),
               'source_collector_sha256':collector_sha, 'classifier_sha256':CLASSIFIER_SHA256,
               'provider_binding_sha256':binding_sha if not legacy else LEGACY_COMMON_SHA256,
               'selection_sha256':sha(selection_raw), 'adapter_sha256':sha(collector_bytes),
               'central_engine_root':str(engine), 'central_run_id_preserved':True,
               'central_detail_files_read_or_changed':False, 'private_lookup_read_or_copied':False,
               'account_or_network_actions_executed':False,
               'public_failure_diagnostics': {'location':'qualified-details-v2-summary.json/errors',
                                              'optional_fields':['structured_category','site_check_diagnostics'],
                                              'raw_messages_or_responses_saved':False},
               'execution_diagnostics': {'location':'qualified-details-v2-summary.json/run_results',
                                         'fields':['execution_stage','detail_tool_called','failure_kind'],
                                         'call_boundary':'get_feed_detail permitted dispatch before Invoke-McpPost',
                                         'provider_internal_retries_counted_as_extra_calls':False},
               'public_output_directory':str(target),
               'execution_command':f'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "{target / "collect-s2-batch.ps1"}" -RunId {run_id} -DelaySeconds 10',
               'source_files':selected['source_files']}
    target.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=target.parent) as scratch:
        staging = Path(scratch) / 'batch'
        staging.mkdir()
        (staging/'detail-selection-v2.json').write_bytes(selection_raw)
        (staging/'session-lifecycle.ps1').write_bytes(classifier_raw)
        (staging/'collect-s2-batch.ps1').write_bytes(collector_bytes)
        (staging/'selection-request.json').write_bytes(spec_raw)
        (staging/'adapter-receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        staging.rename(target)
    return receipt


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--selection',required=True)
    parser.add_argument('--run-id',required=True)
    parser.add_argument('--batch-id',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--engine-root',required=True)
    parser.add_argument('--private-root',required=True)
    parser.add_argument('--as-of')
    args=parser.parse_args()
    receipt=prepare_batch(selection_path=args.selection,run_id=args.run_id,batch_id=args.batch_id,
                          output=args.output,engine_root=args.engine_root,private_root=args.private_root,as_of=args.as_of)
    print(json.dumps(receipt,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
