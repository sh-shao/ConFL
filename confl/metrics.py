"""Ground-truth scoring: method ranking (top-k, MRR, MAP) and component selection. Loaded only after inference."""
from collections import Counter
import json
from pathlib import Path

from .costs import combine_costs


def evaluate_ranking(result, gt, bug_id):
    bugs = json.loads(Path(gt).read_text())
    bug, = [b for b in bugs if b['bugID'] == bug_id]
    ranks = []

    def name_of(method):
        # GT writes constructors as Class::Class(...); ranked ids use <init>.
        owner, name = method.rsplit('#', 1)
        name = name.split('(')[0]
        return owner.replace('$', '.').rsplit('.', 1)[-1] if name == '<init>' else name

    for f in bug['files']:
        for target in f['methods']:
            # A label without a class (one GT entry) belongs to the file's top-level type.
            owner, method = target.rsplit('::', 1) if '::' in target else (Path(f['file']).stem, target)
            owner = owner.replace('::', '.')
            hits = [r['rank'] for r in result['ranking'] if r['file'] == f['file'] and
                    (r['method'].rsplit('#', 1)[0] == owner or r['method'].rsplit('#', 1)[0].endswith('.' + owner)) and
                    name_of(r['method']) == method.split('(')[0]]
            ranks.append(dict(file=f['file'], method=target, rank=min(hits) if hits else None))
    # Collapse overloads/duplicate GT entries under the documented name-level policy.
    unique = {}
    for row in ranks:
        identity = (row['file'], row['method'].split('(', 1)[0])
        previous = unique.get(identity)
        if previous is None or (row['rank'] is not None and (previous['rank'] is None or row['rank'] < previous['rank'])):
            unique[identity] = row
    ranks = list(unique.values())
    hit_ranks = sorted({r['rank'] for r in ranks if r['rank'] is not None})
    average_precision = sum(i / rank for i, rank in enumerate(hit_ranks, 1)) / len(ranks) if ranks else None
    first = min(hit_ranks, default=None)
    return dict(bug_id=bug_id, match_policy='file + class + method name (not exact overload)', methods=ranks,
                top1=first == 1, top3=first is not None and first <= 3, top5=first is not None and first <= 5,
                reciprocal_rank=1/first if first else 0,
                average_precision=average_precision, relevant_method_count=len(ranks),
                ranking_cutoff=result.get('ranking_cutoff', len(result['ranking'])),
                ap_policy='Sum precision at first hit of each distinct relevant method, divided by all distinct GT methods; unreturned methods contribute zero. Ranking is truncated.')



def component_for_file(file):
    file = file.replace('\\', '/').removeprefix('./')
    return file.split('/src/')[0] if '/src/' in file else '.'


def score_selection(bug, selected, modules):
    targets = sorted({component_for_file(f['file']) for f in bug['files']})
    roots = sorted({t.split('/')[0] for t in targets})
    available = {m['name'] for m in modules}
    return dict(target_components=targets, target_roots=roots,
                root_hit=selected[0] in roots,
                leaf_hit=selected[-1] in targets,
                any_target_available=bool(set(targets) & available),
                filtered_target_components=sorted(set(targets) - available))


def summarize(results):
    completed = [r for r in results if r['status'] == 'completed']
    available = [r for r in completed if r['any_target_available']]
    root_failed = [r for r in completed if not r['root_hit']]
    leaf_failed = [r for r in completed if not r['leaf_hit']]
    unavailable = [r for r in completed if not r['any_target_available']]
    method_evaluations = [r['method_evaluation'] for r in completed if 'method_evaluation' in r]
    method_metrics = dict(evaluated_cases=len(method_evaluations),
                          **{key: sum(e[key] for e in method_evaluations)/len(method_evaluations) if method_evaluations else None
                             for key in ('top1', 'top3', 'top5', 'reciprocal_rank')})
    method_metrics['mrr'] = method_metrics['reciprocal_rank']
    ap_values = [e['average_precision'] for e in method_evaluations if e.get('average_precision') is not None]
    method_metrics['map'] = sum(ap_values)/len(ap_values) if ap_values else None
    method_metrics['map_evaluated_cases'] = len(ap_values)
    method_metrics['ranking_cutoffs'] = sorted({e['ranking_cutoff'] for e in method_evaluations if 'ranking_cutoff' in e})
    method_metrics['note'] = 'Completed evaluations only. MRR and MAP use the returned truncated ranking; AP denominator is all distinct GT methods, including unretrieved ones. Missing historical AP is excluded from MAP.'
    partial = [r for r in results if 'partial_method_evaluation' in r and r['status'] != 'completed']
    partial_metrics = dict(cases=len(partial), top5_hits=sum(r['partial_method_evaluation']['top5'] for r in partial), note='Incomplete rankings, excluded from completed method_metrics.')
    return dict(partial_method_metrics=partial_metrics, method_metrics=method_metrics, api_cost=combine_costs([r['api_cost'] for r in results if 'api_cost' in r]) if any('api_cost' in r for r in results) else None, total_cases=len(results), status_counts=dict(Counter(r['status'] for r in results)),
                root_correct_count=len(completed)-len(root_failed), root_failed_count=len(root_failed),
                leaf_correct_count=len(completed)-len(leaf_failed), leaf_failed_count=len(leaf_failed),
                root_failed_cases=[r['bug_id'] for r in root_failed if 'bug_id' in r],
                leaf_failed_cases=[dict(bug_id=r['bug_id'], selected_path=r.get('selected_path', []),
                                       target_components=r.get('target_components', []),
                                       root_hit=r['root_hit'], any_target_available=r['any_target_available'],
                                       reason='selected_wrong_component' if r['any_target_available'] else 'all_target_components_unavailable')
                                   for r in leaf_failed if 'bug_id' in r],
                all_targets_unavailable_count=len(unavailable),
                all_targets_unavailable_cases=[r['bug_id'] for r in unavailable if 'bug_id' in r],
                not_completed_cases=[dict(bug_id=r['bug_id'], status=r['status']) for r in results if r['status'] != 'completed' and 'bug_id' in r],
                evaluated_cases=len(completed),
                root_accuracy=sum(r['root_hit'] for r in completed)/len(completed) if completed else None,
                leaf_accuracy=sum(r['leaf_hit'] for r in completed)/len(completed) if completed else None,
                evaluated_with_target_available=len(available),
                leaf_accuracy_when_target_available=sum(r['leaf_hit'] for r in available)/len(available) if available else None,
                note='Accuracy denominator is completed model evaluations only. Missing inputs and API errors are separate, never silently counted as model mistakes. Any matching GT component is a hit for multi-component bugs.')


def summarize_projects(results):
    return {project: summarize([r for r in results if r['bug_id'].split('_')[0] == project])
            for project in dict.fromkeys(r['bug_id'].split('_')[0] for r in results)}
