"""ConFL: LLM-guided localization of concurrency bugs in Java projects.

  python -m confl localize --source DIR --report FILE      rank suspicious methods for one bug report
  python -m confl evaluate [--projects P ...] [--limit N]  reproduce the dataset results against ground truth
  python -m confl extract  --source DIR --facts DIR        extract facts only (or --dataset for all cases)
  python -m confl report   DIR                             (re)write the explainable report.md of earlier runs
"""
import argparse
import json
from pathlib import Path
import sys

from .config import load_config, kb_options, DEFAULT_MODEL
from .costs import summarize_costs, display_cost
from .evaluation import evaluate_dataset, select_cases, case_inputs
from .extract import ensure_facts, extract_dataset
from .llm import APIModel
from .localize import localize, load_kb, save
from .report import write_reports


def main(argv=None):
    parser = argparse.ArgumentParser(prog='confl', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--config', help=f'Model configuration: a name in configs/ such as claude-opus-5-5, or a JSON file (default: {DEFAULT_MODEL})')
    commands = parser.add_subparsers(dest='command', required=True)

    p = commands.add_parser('localize', parents=[common], help='Rank suspicious methods for one bug report')
    p.add_argument('--source', type=Path, required=True, help='Root of the Java project source tree')
    p.add_argument('--report', type=Path, required=True, help='Bug report text file')
    p.add_argument('--output', type=Path, help='Output directory (default: output/localize/<report name>)')
    p.add_argument('--facts', type=Path, help='Facts directory; extracted there when missing (default: <output>/facts)')
    p.add_argument('--top-k', type=int, help='Number of methods to rank (default: localization.top_k)')
    p.add_argument('--no-report', action='store_true', help='Do not write the explainable report.md')

    p = commands.add_parser('evaluate', parents=[common], help='Localize ground-truth cases and score the rankings')
    add_dataset_arguments(p)
    p.add_argument('--output', type=Path, help='Output directory (default: dataset.output)')
    p.add_argument('--resume', action='store_true', help='Keep completed cases in the output directory; rerun the rest')
    p.add_argument('--extract', action='store_true', help='Extract missing facts before localizing a case')
    p.add_argument('--check', action='store_true', help='Only report missing inputs; no model calls')
    p.add_argument('--no-report', action='store_true', help='Do not write an explainable report.md per case')

    p = commands.add_parser('extract', parents=[common], help='Extract facts without an LLM')
    p.add_argument('--source', type=Path, help='Source tree of one project')
    p.add_argument('--facts', type=Path, help='Facts output directory for --source')
    p.add_argument('--dataset', action='store_true', help='Extract facts for every selected ground-truth case')
    add_dataset_arguments(p)
    p.add_argument('--jobs', type=int, help='Parallel extractor JVMs (default: extraction.jobs)')

    p = commands.add_parser('report', help='Write the explainable report.md from recorded results; no model calls')
    p.add_argument('directory', type=Path, help='A localize output directory, or an evaluate output directory (one report per case)')

    args = parser.parse_args(argv)
    try:
        if args.command == 'report':
            paths = write_reports(args.directory)
            print(f'Wrote {len(paths)} report(s)' + (f': {paths[0]}' if len(paths) == 1 else f' under {args.directory}'))
            return 0
        config = load_config(args.config)
        if getattr(args, 'projects', None):
            config['dataset']['projects'] = args.projects
        if getattr(args, 'limit', None) is not None:
            config['dataset']['limit'] = args.limit
        for key in ('source_root', 'facts_root'):
            if getattr(args, key, None) is not None:
                config['dataset'][key] = getattr(args, key).resolve()
        return dict(localize=run_localize, evaluate=run_evaluate, extract=run_extract)[args.command](args, config, parser)
    except ValueError as exc:
        parser.error(str(exc))


def add_dataset_arguments(p):
    p.add_argument('--projects', nargs='+', help='Projects to include, e.g. --projects grpc vertx (default: dataset.projects or all)')
    p.add_argument('--limit', type=int, help='Only the first N selected cases, in ground-truth order')
    p.add_argument('--source-root', type=Path, help='Dataset sources, <root>/<project>/<case>/ (default: dataset.source_root)')
    p.add_argument('--facts-root', type=Path, help='Dataset facts, <root>/<case>/facts/ (default: dataset.facts_root)')


def make_model(config):
    return APIModel(config['llm'])


def run_localize(args, config, parser):
    if not args.source.is_dir():
        parser.error(f'Source directory not found: {args.source}')
    if not args.report.is_file() or not args.report.read_text(encoding='utf-8').strip():
        parser.error(f'Bug report missing or empty: {args.report}')
    top_k = args.top_k if args.top_k is not None else config['localization']['top_k']
    if top_k < 1:
        parser.error('--top-k must be positive')
    model = make_model(config)
    output = args.output or Path('output/localize') / args.report.stem
    facts = args.facts or output / 'facts'
    record = ensure_facts(args.source, facts, config['extraction'], output / 'extraction.log', args.report.stem)
    if record['status'] == 'generated':
        print(f'Extracted facts to {facts}')
    kb = load_kb(facts, args.source, kb_options(config), print)
    result = localize(kb, args.report.read_text(encoding='utf-8'), output, model, top_k, print, explain=not args.no_report)
    print('API estimate: ' + display_cost(summarize_costs(model.calls)))
    if result['status'] != 'completed':
        print(f"Localization {result['status']}: {result.get('error', '')}", file=sys.stderr)
        return 1
    print(f"\nTop {len(result['ranking'])} suspicious methods (details: {output / 'result.json'}):")
    for row in result['ranking']:
        print(f"  {row['rank']}. {row['method']}\n     {row['file']}")
    if not args.no_report:
        print(f"Explainable report: {output / 'report.md'}")
    return 0


def run_evaluate(args, config, parser):
    output = args.output or config['dataset']['output']
    if args.check:
        if config['dataset']['source_root'] is None:
            parser.error('Set dataset.source_root in the configuration')
        bugs = select_cases(json.loads(config['dataset']['ground_truth'].read_text()), config['dataset']['projects'], config['dataset']['limit'])
        manifest = [case_inputs(bug, config['dataset']) for bug in bugs]
        save(Path(output) / 'manifest.json', manifest)
        missing = [r for r in manifest if r['missing']]
        print(f'{len(manifest)} cases; {len(manifest) - len(missing)} ready')
        for row in missing:
            print(f"  {row['bug_id']}: missing {', '.join(row['missing'])}")
        return 0
    results = evaluate_dataset(config, make_model(config), output, resume=args.resume, extract=args.extract, explain=not args.no_report)
    summary = json.loads((Path(output) / 'summary.json').read_text())
    print(json.dumps(dict(cases=len(results), status_counts=summary['status_counts'], method_metrics=summary['method_metrics']), indent=2))
    return 0


def run_extract(args, config, parser):
    settings = dict(config['extraction'])
    if args.jobs is not None:
        if args.jobs < 1:
            parser.error('--jobs must be positive')
        settings['jobs'] = args.jobs
    if args.dataset == bool(args.source or args.facts):
        parser.error('Use either --source with --facts, or --dataset')
    if not args.dataset:
        if not (args.source and args.facts):
            parser.error('--source and --facts are both required')
        record = ensure_facts(args.source, args.facts, settings, args.facts.parent / 'extraction.log', args.source.name)
        print(f"{record['status']}: {args.facts}")
        return 0
    dataset = config['dataset']
    if dataset['source_root'] is None:
        parser.error('Set dataset.source_root in the configuration')
    bugs = select_cases(json.loads(dataset['ground_truth'].read_text()), dataset['projects'], dataset['limit'])
    cases = [(r['bug_id'], r['source'], r['facts']) for r in (case_inputs(b, dataset) for b in bugs)]
    log_dir = dataset['facts_root'] / '_logs'
    results = extract_dataset(cases, settings, log_dir, settings['jobs'])
    save(log_dir / 'results.json', results)
    failed = [r for r in results if r['status'] not in ('generated', 'skipped_valid')]
    print(f'{len(results) - len(failed)}/{len(results)} cases have valid facts; logs in {log_dir}')
    return 1 if failed else 0
