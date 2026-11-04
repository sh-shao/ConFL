"""Dataset evaluation: localize every selected ground-truth case, then score rankings and component choices.

Layout: <source_root>/<project>/<case>/ (source), <reports_dir>/<project>/<case>.txt (bug report),
<facts_root>/<case>/facts/ (extracted facts). Ground truth is read only after a case's inference finishes.
"""
from collections import Counter
import json
from pathlib import Path
import time

from .config import kb_options
from .costs import summarize_costs, display_cost
from .extract import ensure_facts
from .localize import localize, load_kb, save
from .report import write_report
from .metrics import evaluate_ranking, score_selection, summarize, summarize_projects


def select_cases(bugs, projects=None, limit=None):
    """Ground-truth bugs of the given projects (all when None), in file order, truncated to limit."""
    available = list(dict.fromkeys(b['bugID'].split('_')[0] for b in bugs))
    if projects is None:
        projects = available
    if not isinstance(projects, list) or not projects or any(not isinstance(x, str) for x in projects):
        raise ValueError('projects must be null or a nonempty list of project names')
    if len(set(projects)) != len(projects):
        raise ValueError('projects contains duplicate names')
    unknown = set(projects) - set(available)
    if unknown:
        raise ValueError('Unknown project(s): ' + ', '.join(sorted(unknown)))
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError('limit must be a positive integer')
    selected = [b for project in projects for b in bugs if b['bugID'].split('_')[0] == project]
    return selected if limit is None else selected[:limit]


def case_inputs(bug, dataset):
    """Paths of one case and which inputs are missing (facts may still be extracted)."""
    case = bug['bugID']
    project = case.split('_')[0]
    source = dataset['source_root'] / project / case
    report = dataset['reports_dir'] / project / (case + '.txt')
    facts = dataset['facts_root'] / case / 'facts'
    missing = [name for name, ok in (('source', source.is_dir()),
                                     ('report', report.is_file() and bool(report.read_text(encoding='utf-8').strip())),
                                     ('facts', (facts / 'schema-version.txt').is_file())) if not ok]
    return dict(bug_id=case, source=str(source), report=str(report), facts=str(facts), missing=missing)


def evaluate_dataset(config, model, output, resume=False, extract=False, progress=print, explain=True):
    """Run and score all selected cases; writes results.json, summary.json and project_summary.json to output,
    and, unless explain is False, an explainable report.md per case that marks ground-truth matches."""
    dataset = config['dataset']
    if dataset['source_root'] is None:
        raise ValueError('Set dataset.source_root in the configuration')
    bugs = select_cases(json.loads(dataset['ground_truth'].read_text()), dataset['projects'], dataset['limit'])
    manifest = [case_inputs(bug, dataset) for bug in bugs]
    output = Path(output)
    save(output / 'manifest.json', manifest)
    previous = {}
    if resume and (output / 'results.json').exists():
        previous = {r['bug_id']: r for r in json.loads((output / 'results.json').read_text())}
        if (output / 'api_usage.json').exists():
            model.calls = json.loads((output / 'api_usage.json').read_text())
    totals = Counter(r['bug_id'].split('_')[0] for r in manifest)
    done = Counter()
    results = []
    log = output / 'progress.log'
    log.write_text('')

    def report(message):
        progress(message)
        with log.open('a', encoding='utf-8') as stream:
            stream.write(message + '\n')

    def finish(result, started, call_start):
        result['elapsed_seconds'] = round(time.monotonic() - started, 2)
        result['api_cost'] = summarize_costs(model.calls[call_start:])
        total_cost = summarize_costs(model.calls)
        save(output / 'cost_summary.json', total_cost)
        save(output / 'api_usage.json', model.calls)
        save(output / 'cases' / result['bug_id'] / 'result.json', result)
        report(f"  API estimate: case {display_cost(result['api_cost'])} | run {display_cost(total_cost)}")
        results.append(result)
        save(output / 'results.json', results)
        save(output / 'summary.json', summarize(results))
        by_project = summarize_projects(results)
        save(output / 'project_summary.json', by_project)
        project = result['bug_id'].split('_')[0]
        done[project] += 1
        detail = result['status']
        if result['status'] == 'completed':
            detail += f" root_hit={result['root_hit']} leaf_hit={result['leaf_hit']}"
        if 'method_evaluation' in result:
            e = result['method_evaluation']
            detail += f" top1={e['top1']} top3={e['top3']} top5={e['top5']}"
        report(f"  Result: {detail} ({result['elapsed_seconds']:.1f}s)")
        if done[project] == totals[project]:
            m = by_project[project]['method_metrics']
            fmt = lambda v: 'N/A' if v is None else f'{v:.1%}'
            report(f"Project {project} finished: {m['evaluated_cases']}/{totals[project]} evaluated, "
                   f"top1={fmt(m['top1'])} top5={fmt(m['top5'])} MRR={'N/A' if m['mrr'] is None else round(m['mrr'], 3)}")

    report(f"Cases: {len(manifest)} | Projects: {', '.join(totals)} | Model: {model.model}")
    for record, bug in zip(manifest, bugs):
        case = record['bug_id']
        if previous.get(case, {}).get('status') == 'completed':
            results.append(previous[case])
            done[case.split('_')[0]] += 1
            report(f'  Resume: skipped completed {case}')
            continue
        call_start, started = len(model.calls), time.monotonic()
        report(f"[{len(results)+1}/{len(manifest)}] {case}")
        out = output / 'cases' / case
        result = dict(bug_id=case)
        if 'source' in record['missing'] or 'report' in record['missing']:
            result['status'] = 'missing_' + record['missing'][0]
        elif 'facts' in record['missing'] and not extract:
            result['status'] = 'missing_facts'
        else:
            try:
                if 'facts' in record['missing']:
                    report('  Extracting facts')
                    ensure_facts(record['source'], record['facts'], config['extraction'], out / 'extraction.log', case)
                kb = load_kb(record['facts'], record['source'], kb_options(config), report)
                prediction = localize(kb, Path(record['report']).read_text(), out, model, config['localization']['top_k'], report, explain=False)
                result.update(prediction)
                if prediction['status'] == 'completed':
                    result['method_evaluation'] = evaluate_ranking(prediction, dataset['ground_truth'], case)
                    result.update(score_selection(bug, prediction['selected_path'], kb.list_modules()))
                elif prediction.get('partial_ranking'):
                    partial = dict(prediction, ranking=prediction['partial_ranking'], ranking_cutoff=config['localization']['top_k'])
                    result['partial_method_evaluation'] = evaluate_ranking(partial, dataset['ground_truth'], case)
                if 'method_evaluation' in result or 'partial_method_evaluation' in result:
                    save(out / 'evaluation.json', result.get('method_evaluation') or result['partial_method_evaluation'])
            except (ValueError, OSError) as exc:
                result.update(status='input_or_protocol_error', error=str(exc))
        finish(result, started, call_start)
        if explain and (out / 'trace.json').exists():
            write_report(out)
        if 'API_QUOTA_EXHAUSTED' in result.get('error', '') or 'API HTTP 429' in result.get('error', ''):
            report('Stopping after quota/rate-limit failure. Rerun with --resume once the API limit is resolved.')
            for pending in manifest[len(results):]:
                results.append(previous.get(pending['bug_id']) or dict(bug_id=pending['bug_id'], status='not_run_api_limit'))
            save(output / 'results.json', results)
            save(output / 'summary.json', summarize(results))
            save(output / 'project_summary.json', summarize_projects(results))
            break
    return results

