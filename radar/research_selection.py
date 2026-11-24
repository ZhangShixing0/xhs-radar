"""Deterministic sample selection, separate from AI interpretation."""
from datetime import datetime, timezone
import json


def instant(value):
    if not isinstance(value,str):
        raise ValueError('Missing or invalid timestamp')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Timestamp must include timezone')
    return parsed


def _snapshot_content(note):
    """Compare observed content/counts, not import provenance or URL aliases.

    Extra content fields participate by default. Exact normalized counts are
    compared separately from raw display strings. Equivalent timezone notation
    does not create a publication-time conflict.
    """
    content={k:v for k,v in note.items()
             if k not in ('provenance','url','raw_metrics','collected_at','published_at')}
    content['published_at']=instant(note['published_at']).astimezone(timezone.utc).isoformat() if note.get('published_at') else None
    return json.dumps(content,sort_keys=True,ensure_ascii=False)


def select_notes(notes, policy):
    """Select latest valid observations, quarantining ambiguous note identities.

    Reject locators use zero-based indices into the original input. A conflicting
    author (even on an unusable metric snapshot), or conflicting in-window
    observations at the same instant, quarantines the entire note ID. A newer
    in-window snapshot with invalid configured metrics does not silently replace
    an older valid one: selection keeps the old behavior and emits a quality flag.
    """
    start, end = instant(policy['since']), instant(policy['as_of'])
    if start > end:
        raise ValueError('Invalid observation window')
    include_undated = policy.get('include_undated', False)
    if type(include_undated) is not bool or (include_undated and start != datetime(1970, 1, 1, tzinfo=timezone.utc)):
        raise ValueError('Undated notes require an all-history window')
    thresholds = {k: policy[k] for k in ('min_likes','min_saves') if k in policy}
    if not thresholds or any(type(v) is not int or v < 0 for v in thresholds.values()):
        raise ValueError('Provide nonnegative integer engagement thresholds')
    min_authors = policy.get('min_authors', 3)
    if type(min_authors) is not int or min_authors < 1:
        raise ValueError('min_authors must be positive')
    mode=policy.get('threshold_mode','any')
    if mode not in ('any','all'):
        raise ValueError('threshold_mode must be any or all')
    groups={}; rejected={}; chosen=[]; quality_flags=[]

    def reject(index,n,reason,**detail):
        nid=n.get('id') if isinstance(n,dict) else None
        rejected[index]={'id':nid if isinstance(nid,str) else None,
                         'input_index':index,'reason':reason,**detail}

    for index,n in enumerate(notes):
        if not isinstance(n,dict):
            reject(index,n,'invalid_note_object')
            continue
        if any(not isinstance(n.get(k),str) or not n[k].strip() for k in ('id','author_id','url')):
            reject(index,n,'missing_identity_or_source')
            continue
        groups.setdefault(n['id'],[]).append((index,n))

    for nid,group in sorted(groups.items()):
        if len({n['author_id'] for _,n in group})>1:
            indices=[i for i,_ in group]
            for i,n in group:
                reject(i,n,'conflicting_author',conflict_indices=indices,
                       author_id=n['author_id'],collected_at=n.get('collected_at'))
            continue

        observations=[]; by_time={}
        for i,n in group:
            try:
                captured=instant(n['collected_at'])
                pub=None if include_undated and n.get('published_at') is None else instant(n.get('published_at'))
                if not start <= captured <= end or (pub is not None and not start <= pub <= captured):
                    raise ValueError('outside_window_or_invalid_capture')
            except (ValueError,KeyError,TypeError) as e:
                reject(i,n,str(e))
                continue
            observations.append((i,n,captured))
            by_time.setdefault(captured,[]).append((i,n))

        conflicts=[]
        for snapshots in by_time.values():
            if len(snapshots)>1 and len({_snapshot_content(n) for _,n in snapshots})>1:
                conflicts.extend(i for i,_ in snapshots)
        if conflicts:
            for i,n in group:
                reject(i,n,'conflicting_snapshot',conflict_indices=sorted(conflicts),collected_at=n.get('collected_at'))
            continue

        valid=[]; unusable=[]
        for i,n,captured in observations:
            if any(type(n.get(k.removeprefix('min_'))) is not int or n[k.removeprefix('min_')] < 0 for k in thresholds):
                reject(i,n,'missing_or_invalid_metrics')
                unusable.append((i,n,captured))
            else:
                valid.append((i,n,captured))
        if not valid:
            continue
        latest=max(captured for _,_,captured in valid)
        # Deterministic representative if only provenance / timezone text differs.
        selected_index,n,_=min((row for row in valid if row[2]==latest),
                               key=lambda row:json.dumps(row[1],sort_keys=True,ensure_ascii=False))
        for i,other,_ in valid:
            if i!=selected_index:
                reject(i,other,'duplicate_snapshot')
        newer=[i for i,_,captured in unusable if captured>latest]
        if newer:
            quality_flags.append({'id':nid,'reason':'newer_snapshot_unusable',
                                  'selected_input_index':selected_index,
                                  'selected_collected_at':n['collected_at'],
                                  'unusable_input_indices':newer})
        checks=[n[k.removeprefix('min_')]>=v for k,v in thresholds.items()]
        if (all(checks) if mode=='all' else any(checks)):
            chosen.append(n)
        else:
            reject(selected_index,n,'below_threshold')
    authors=len({n['author_id'] for n in chosen})
    return {'notes':chosen,'sample_count':len(chosen),'author_count':authors,
            'sufficient_authors':authors>=min_authors,
            'rejected':[rejected[i] for i in sorted(rejected)],
            'quality_flags':quality_flags,'policy':policy}
