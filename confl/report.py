"""Explainable localization report: renders a localization record (result.json, trace.json, DSL evidence) as report.md.

The report is built from what the run recorded; it makes no model calls. For every ranked method it shows the
model's explanation next to the concurrency evidence the interaction DSL holds for it: which thread entries
reach it, which shared resources it accesses together with the other selected thread, and whether those
accesses share a lock.
"""
import json
from pathlib import Path
import re

RESOURCE_LIMIT = 8  # shared resources shown per ranked method, unprotected first
PARTNER_LIMIT = 3  # accesses of the other thread shown per resource
ENTRY_LIMIT = 5  # reaching thread entries listed per method
SNIPPET_LINES = 40  # source lines shown per method
REPORT_LINES = 25  # bug report lines quoted at the top
NAVIGATION_STAGES = {'component': 'Component', 'package': 'Package', 'thread_entry': 'Thread entry',
                     'interacting_entry': 'Interacting entry'}


def short(name):
    """Readable method/entry name: packages dropped, anonymous-class paths reduced to file:line."""
    if not name:
        return ''
    name = re.sub(r'@[^#()@]*/([^/#()]+?\.java):(\d+):\d+', r'@\1:\2', name)
    return re.sub(r'\b(?:[a-z_][\w$]*\.)+(?=[A-Z_$])', '', name)


def _writes(action):
    return action != 'R'


def _initializer(method):
    """Constructors and initializers run before the object is shared, so their writes do not conflict."""
    return any(x in method for x in ('#<init>', '#<instance-init>', '#<clinit>'))


def _location(site):
    file, _, rest = site.rpartition('/')[2].partition(':')
    return f"{file}:{rest.split(':')[0]}" if rest else site


def resource_evidence(interaction, method):
    """This method's accesses to one shared resource, the other thread's accesses to it, and whether every
    conflicting pair (at least one write, initializers excluded) holds a common lock."""
    mine, other = {}, {}
    for c in interaction['contexts']:
        if not any(a['method'] == method for a in c['accesses']):
            continue
        for a in c['accesses']:
            if a['method'] == method:
                mine.setdefault((a['location'], a['action']), a)
        for o in interaction['contexts']:
            if o is not c:
                for b in o['accesses']:
                    other.setdefault((b['method'], b['location'], b['action']), b)
    mine, other = list(mine.values()), list(other.values())
    locks = lambda x: set(x.get('guarded_by', []))
    conflicts = [(a, b) for a in mine for b in other if not _initializer(a['method']) and not _initializer(b['method'])
                 and (_writes(a['action']) or _writes(b['action']))]
    unguarded = [(a, b) for a, b in conflicts if not locks(a) & locks(b)]
    common = set.intersection(*(locks(a) & locks(b) for a, b in conflicts)) if conflicts and not unguarded else set()
    bad = {id(b) for _, b in unguarded}
    conflicting = {id(b) for _, b in conflicts}
    other.sort(key=lambda b: (id(b) not in bad, id(b) not in conflicting, b['method']))
    status = 'no_common_lock' if unguarded else 'common_lock' if conflicts else 'no_conflict'
    return dict(resource=interaction['shared_resource'], status=status, common_locks=sorted(common),
                conflicting_pairs=len(conflicts), pairs_without_common_lock=len(unguarded),
                accesses=[dict(a, location=_location(a['location'])) for a in mine[:2]],
                other_thread=[dict(b, location=_location(b['location']), conflict=id(b) in conflicting, unguarded=id(b) in bad)
                              for b in other[:PARTNER_LIMIT]],
                other_thread_total=len(other))


STATUS_ORDER = {'no_common_lock': 0, 'common_lock': 1, 'no_conflict': 2}


def collect_dsl_evidence(interactions, ranking, entry_pair=None, kb=None, pool=()):
    """Per ranked method: DSL interactions on the selected entry pair and, with kb, its reachability summary."""
    sources = {r['name']: r.get('evidence_sources', []) for r in pool}
    kinds = {}
    if kb is not None:
        for entry, kind in kb.facts['EntryKind']:
            kinds.setdefault(entry, set()).add(kind)
    declared = lambda e: 'user-declared' in kinds.get(e, ())
    methods = []
    for row in ranking:
        m = row['method']
        resources = [resource_evidence(i, m) for i in interactions
                     if any(a['method'] == m for c in i['contexts'] for a in c['accesses'])]
        resources.sort(key=lambda r: (STATUS_ORDER[r['status']], r['resource']))
        record = dict(rank=row['rank'], method=m, evidence_sources=sources.get(m, []),
                      shared_resource_count=len(resources),
                      conflict_count=sum(r['status'] != 'no_conflict' for r in resources),
                      unprotected_count=sum(r['status'] == 'no_common_lock' for r in resources),
                      resources=resources[:RESOURCE_LIMIT])
        if kb is not None and m in kb.methods:
            summary = kb.get_method_summary(m)
            reaching = sorted(summary['thread_entries'], key=lambda x: not declared(x))  # user-declared entries first
            record.update(thread_entry_count=len(reaching), thread_entries=reaching[:ENTRY_LIMIT],
                          declared_thread_entries=[x for x in reaching if declared(x)][:ENTRY_LIMIT],
                          access_count=len(summary['accesses']), dsl_suspicion_rank=summary.get('dsl_suspicion_rank'),
                          dsl_member_count=summary.get('dsl_member_count'))
        methods.append(record)
    shown = set(entry_pair or ()) | {x for r in methods for x in r.get('thread_entries', ())}
    return dict(entry_pair=entry_pair, interaction_count=len(interactions),
                entry_kinds={e: sorted(kinds[e]) for e in sorted(shown) if e in kinds},
                declared_entry_rules=getattr(kb, 'declared_entry_matches', {}),
                methods=methods,
                note='MAY evidence: accesses come from static reachability with unresolved-call fallbacks. A lock counts only '
                     'where the accessing method holds it lexically (synchronized block or method); locks held by callers are '
                     'not tracked, so "no common lock" can be over-reported. Writes in constructors and initializers are not conflicts.')


def _load(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def _decisions(trace):
    """Navigation choices, and per final ranking decision the tool calls made since the previous one."""
    navigation, ranks, tools = [], [], []
    for event in trace:
        stage = event.get('stage') or event.get('request', {}).get('stage')
        if 'tool' in event and stage == 'method_ranking':
            tools.append(f"{event['tool']}({short(next(iter(event.get('arguments', {}).values()), ''))})")
        elif 'request' in event:
            response = event.get('response') or {}
            if stage in NAVIGATION_STAGES:
                navigation.append((stage, response.get('name'), response.get('explanation', '')))
            elif stage == 'method_ranking':
                ranks.append((response.get('name'), list(dict.fromkeys(tools))))
                tools = []
    return navigation, ranks


def _quote(text, limit=None):
    lines = text.strip().splitlines()
    more = limit is not None and len(lines) > limit
    lines = lines[:limit] if limit else lines
    return '\n'.join('> ' + line if line.strip() else '>' for line in lines) + ('\n>\n> …' if more else '')


def _cell(text):
    return str(text).replace('|', '\\|').replace('\n', ' ')


def _locks(locks):
    return ', '.join(f'`{short(x)}`' for x in locks) or 'none'


def render(out):
    """Markdown report for one localization output directory."""
    out = Path(out)
    result = _load(out / 'result.json')
    if result is None:
        raise ValueError(f'No result.json in {out}')
    trace = _load(out / 'trace.json', [])
    ranking = result.get('ranking') or result.get('partial_ranking') or []
    interactions = None
    evidence = _load(out / 'dsl_evidence.json')
    if evidence is None:
        interactions = _load(out / 'selected_interactions.json', [])
        evidence = collect_dsl_evidence(interactions, ranking, result.get('entry_pair'),
                                        pool=_load(out / 'ranking_candidates.json', []))
    by_method = {e['method']: e for e in evidence['methods']}
    evaluation = _load(out / 'evaluation.json')
    hits = {}
    for gt in (evaluation or {}).get('methods', []):
        if gt.get('rank'):
            hits.setdefault(gt['rank'], []).append(gt['method'])
    navigation, rank_decisions = _decisions(trace)
    report = (out / 'bug_report.txt').read_text(encoding='utf-8') if (out / 'bug_report.txt').exists() else \
        next((e['request'].get('bug_report', '') for e in trace if 'request' in e), '')
    cost = _load(out / 'cost_summary.json') or result.get('api_cost') or {}
    pair = evidence.get('entry_pair') or result.get('entry_pair')

    lines = ['# ConFL localization report', '']
    title = result.get('bug_id') or out.name
    status = result.get('status', 'completed')
    lines += [f'**Case:** `{title}`  ', f'**Status:** {status}' + (f" ({result['error']})" if result.get('error') else '') + '  ']
    if cost.get('estimated_cost_usd') is not None:
        lines.append(f"**Model calls:** {cost.get('calls', '?')}, estimated ${cost['estimated_cost_usd']:.2f}  ")
    lines.append('')
    if report:
        lines += ['## Bug report', '', _quote(report, REPORT_LINES), '']

    lines += ['## Result', '']
    if not ranking:
        lines += ['No method was ranked.', '']
    else:
        if status != 'completed':
            lines += ['The run stopped early; these are the ranks chosen before it stopped.', '']
        header = '| Rank | Method | File | Concurrency evidence (DSL) |' + (' Ground truth |' if evaluation else '')
        lines += [header, '|---|---|---|---|' + ('---|' if evaluation else '')]
        for row in ranking:
            e = by_method.get(row['method'], {})
            dsl = (f"{e['conflict_count']} conflicting shared resources, {e['unprotected_count']} without a common lock"
                   if e.get('conflict_count') else 'shared resources, no conflicting access' if e.get('shared_resource_count')
                   else 'none on the selected thread pair')
            gt = (' ✔ match |' if hits.get(row['rank']) else ' |') if evaluation else ''
            lines.append(f"| {row['rank']} | `{_cell(short(row['method']))}` | {_cell(row.get('file', ''))} | {dsl} |{gt}")
        lines.append('')

    lines += ['## How ConFL narrowed the search', '',
              'Each step chose among candidates the concurrency knowledge base offered; the reasons are the model\'s.', '']
    for stage, name, why in navigation:
        lines += [f'- **{NAVIGATION_STAGES[stage]}:** `{short(name)}`', f'  {why.strip()}' if why else '']
    inventory = _load(out / 'knowledge_inventory.json') or {}
    summary = _load(out / 'interaction_summary.json') or {}
    if inventory or summary:
        lines += ['', 'Search space: ' + ', '.join(x for x in (
            f"{inventory['methods']:,} methods" if 'methods' in inventory else '',
            f"{inventory['entries']:,} thread entries" if 'entries' in inventory else '',
            f"{summary['candidate_pool_size']:,} candidate methods after component selection" if 'candidate_pool_size' in summary else '',
            f"{summary['shortlist_size']} shown to the model for ranking" if 'shortlist_size' in summary else '') if x) + '.']
    lines.append('')

    lines += ['## Thread interaction (DSL)', '']
    if pair:
        kinds = evidence.get('entry_kinds', {})
        kind = lambda e: f" ({', '.join(kinds[e])})" if kinds.get(e) else ''
        lines += [f'- **Thread A:** `{short(pair[0])}`{kind(pair[0])}', f'- **Thread B:** `{short(pair[1])}`{kind(pair[1])}',
                  f"- {evidence['interaction_count']:,} potential interactions: shared resources both threads can reach and at least one writes.", '',
                  'For each ranked method below, the DSL lists the resources it shares with the other thread and whether every '
                  'conflicting pair of accesses (at least one a write) holds a common lock. Accesses are *possible*, from static '
                  'reachability; they are evidence for the model, not proven races.', '']
    else:
        lines += ['No pair of thread entries with a shared write was found in the searched components; the ranking '
                  'rests on source evidence only and no concurrency interaction is claimed.', '']
    if evidence.get('declared_entry_rules'):
        lines += ['User-declared thread entries (`knowledge_base.extra_entries`):', '']
        lines += [f"- `{rule}`: {count} method{'s' if count != 1 else ''}" + (' — matched nothing; check the name' if not count else '')
                  for rule, count in sorted(evidence['declared_entry_rules'].items())]
        lines.append('')

    hypothesis = (_load(out / 'fault_hypothesis.json') or {}).get('hypothesis')
    if hypothesis:
        lines += ['## Fault hypotheses', '', 'Formed before ranking and tested against source while ranking.', '',
                  hypothesis.strip(), '']

    lines += ['## Suspicious methods', '']
    tools_by_method = {}
    for name, tools in rank_decisions:
        tools_by_method.setdefault(name, tools)
    for row in ranking:
        m = row['method']
        e = by_method.get(m, {})
        lines += [f"### {row['rank']}. `{short(m)}`", '']
        snippet = _load(out / f"code_rank_{row['rank']}.json") or {}
        where = row.get('file', '') + (f":{snippet['start_line']}" if snippet.get('start_line') else '')
        lines += [f'`{where}`  ', f'Full ID: `{m}`', '']
        if hits.get(row['rank']):
            lines += [f"**Ground truth match:** {', '.join(f'`{x}`' for x in hits[row['rank']])}", '']
        lines += ['**Why it is suspicious (model explanation)**', '', row.get('explanation', '').strip() or '—', '']
        lines += ['**Concurrency evidence from the DSL**', '']
        facts = []
        if 'thread_entry_count' in e:
            declared = set(e.get('declared_thread_entries', ()))
            entries = ', '.join(f'`{short(x)}`' + (' (user-declared)' if x in declared else '') for x in e['thread_entries'])
            more = ' …' if e['thread_entry_count'] > len(e['thread_entries']) else ''
            facts.append(f"Reached from {e['thread_entry_count']} thread entr{'y' if e['thread_entry_count'] == 1 else 'ies'}"
                         + (f': {entries}{more}' if entries else ''))
            facts.append(f"Accesses {e['access_count']} shared-state location{'s' if e['access_count'] != 1 else ''}")
        if e.get('dsl_suspicion_rank'):
            facts.append(f"Static DSL suspicion rank {e['dsl_suspicion_rank']:,} of {e['dsl_member_count']:,} methods")
        if e.get('evidence_sources'):
            facts.append('Retrieved because of: ' + ', '.join(s.replace('_', ' ') for s in e['evidence_sources']))
        lines += [f'- {f}' for f in facts]
        if e.get('resources'):
            lines += [f"- Shares {e['shared_resource_count']} resource{'s' if e['shared_resource_count'] != 1 else ''} with the other selected thread; "
                      f"{e['conflict_count']} with conflicting accesses, {e['unprotected_count']} of them without a common lock:", '',
                      '| Shared resource | This method | Other thread | Protection |', '|---|---|---|---|']
            for r in e['resources']:
                held = lambda a: f" holding {_locks(a['guarded_by'])}" if a.get('guarded_by') else ''
                this = '<br>'.join(f"{a['action']} {_cell(a['location'])}{held(a)}" for a in r['accesses'])
                other = '<br>'.join(('⚠ ' if b['unguarded'] else '') + f"`{_cell(short(b['method']))}` {b['action']} {_cell(b['location'])}{held(b)}"
                                    for b in r['other_thread'])
                if r['other_thread_total'] > len(r['other_thread']):
                    other += f"<br>+{r['other_thread_total'] - len(r['other_thread'])} more"
                protection = {'no_common_lock': f"**no common lock** ({r['pairs_without_common_lock']} of {r['conflicting_pairs']} conflicting pairs)",
                              'common_lock': 'common lock ' + _locks(r['common_locks']),
                              'no_conflict': 'reads only (after construction)'}[r['status']]
                lines.append(f"| `{_cell(short(r['resource']))}` | {this} | {other} | {protection} |")
            if e['shared_resource_count'] > len(e['resources']):
                lines.append(f"\n{e['shared_resource_count'] - len(e['resources'])} more resources in `dsl_evidence.json`. ⚠ marks an access with no lock in common with this method's.")
            else:
                lines.append("\n⚠ marks an access with no lock in common with this method's.")
        elif pair:
            lines.append('- No interaction with the other selected thread in the DSL; the rank rests on the explanation and source.')
        lines.append('')
        if tools_by_method.get(m):
            lines += ['**Evidence the model inspected before choosing it:** ' + ', '.join(f'`{_cell(t)}`' for t in tools_by_method[m]), '']
        if snippet.get('lines'):
            code = snippet['lines'][:SNIPPET_LINES]
            lines += ['<details><summary>Source</summary>', '', '```java', *code,
                      *(['// …'] if len(snippet['lines']) > SNIPPET_LINES else []), '```', '', '</details>', '']

    if evaluation:
        lines += ['## Ground truth', '', '| Ground-truth method | File | Rank |', '|---|---|---|']
        for gt in evaluation.get('methods', []):
            lines.append(f"| `{_cell(gt['method'])}` | {_cell(gt['file'])} | {gt.get('rank') or 'not ranked'} |")
        lines += ['', f"Reciprocal rank {evaluation.get('reciprocal_rank', 0):.2f}, average precision "
                      f"{evaluation.get('average_precision', 0):.2f} ({evaluation.get('match_policy', '')}).", '']

    lines += ['## Limitations', '']
    lines += [f'- {x}' for x in result.get('limitations', []) if not x.startswith('Offline mode')]  # removed mode, older records
    lines += ['- ' + evidence['note'], '',
              'Files in this directory: `result.json` (ranking), `trace.json` (every model step and tool call), '
              '`dsl_evidence.json`, `selected_interactions.json` (full DSL of the thread pair).', '']
    return '\n'.join(lines)


def write_report(out):
    """Render out/report.md; returns its path."""
    path = Path(out) / 'report.md'
    path.write_text(render(out), encoding='utf-8')
    return path


def write_reports(directory):
    """report.md for a localization directory, or for every case of an evaluation directory."""
    directory = Path(directory)
    if (directory / 'result.json').exists() and not (directory / 'cases').is_dir():
        return [write_report(directory)]
    cases = sorted(p for p in (directory / 'cases').glob('*') if (p / 'result.json').exists())
    if not cases:
        raise ValueError(f'No localization results under {directory}')
    return [write_report(p) for p in cases]
