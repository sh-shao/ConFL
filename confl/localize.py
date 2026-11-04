"""Localize one bug: knowledge base from facts + source, then the LLM-guided hierarchical search. Used by the
`localize` command and, unchanged, by dataset evaluation (which scores the result afterwards)."""
import json
from pathlib import Path

from .kb import CKB
from .llm import ModelError
from .report import collect_dsl_evidence, write_report
from .runner import Runner


def save(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def load_kb(facts, source, options, progress=None):
    if progress:
        progress('  Loading facts and computing reachability')
    kb = CKB(facts, source, **options)
    if progress:
        for rule, count in sorted(kb.declared_entry_matches.items()):
            progress(f'  Declared entry {rule}: {count} method(s)' + (' -- check the name' if count == 0 else ''))
    return kb


def localize(kb, report, out, model, top_k=5, progress=None, explain=True):
    """Rank up to top_k suspicious methods for report. Never reads ground truth.

    Returns the result dict saved as out/result.json; status is completed, no_valid_candidate or model_error.
    A run that stops early keeps the ranks chosen so far as partial_ranking. The DSL evidence of the ranked methods
    is saved as dsl_evidence.json and, unless explain is False, rendered with the rest of the record as report.md.
    """
    out = Path(out)
    save(out / 'component_filter.json', kb.module_filter_report())
    save(out / 'component_knowledge.json', dict(modules=kb.list_modules(), root_components=kb.list_components()))
    save(out / 'knowledge_inventory.json', dict(methods=len(kb.methods), entries=len(kb.entries), reachability=len(kb.reach)))
    (out / 'partial_result.json').unlink(missing_ok=True)
    (out / 'bug_report.txt').write_text(report, encoding='utf-8')
    runner = Runner(kb, report, out, model=model, top_k=top_k, progress=progress)
    try:
        result = runner.run()
        result.update(status='completed', selected_path=result['component_path'])
    except ModelError as exc:
        result = dict(status='model_error', error=str(exc))
    except ValueError as exc:
        result = dict(status='no_valid_candidate', error=str(exc))
    if result['status'] != 'completed' and (out / 'partial_result.json').exists():
        result['partial_ranking'] = json.loads((out / 'partial_result.json').read_text())['ranking']
    save(out / 'result.json', result)
    ranking = result.get('ranking') or result.get('partial_ranking') or []
    if hasattr(runner, 'ranking_interactions'):
        save(out / 'dsl_evidence.json', collect_dsl_evidence(runner.ranking_interactions, ranking, runner.selected_pair,
                                                             kb, getattr(runner, 'full_method_pool', ())))
    if explain:
        write_report(out)
    return result
