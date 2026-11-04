"""Java facts extraction for one source tree or a dataset of cases. No LLM, report or ground truth involved."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import subprocess
import tempfile

from .dsl import load_facts
from .prefilter import select_sources


def valid_facts(path):
    try:
        load_facts(path)
        return True
    except (OSError, ValueError):
        return False


def extract(source, facts, settings, log, case_id='case'):
    """Extract facts for source into facts (a directory that must not exist yet).

    Facts are published atomically after validation, so an interrupted run never looks complete.
    Returns a status record: generated, no_sources_selected or extraction_error.
    """
    source, facts, log = Path(source).resolve(), Path(facts).resolve(), Path(log)
    jar = settings['extractor_jar']
    if jar is None or not Path(jar).is_file():
        raise ValueError(f'Extractor JAR not found: {jar}; build it with: mvn -f FactsExtractor/pom.xml package')
    record = dict(case_id=case_id, source=str(source), facts=str(facts))
    facts.parent.mkdir(parents=True, exist_ok=True)
    log.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.extract-', dir=facts.parent) as tmp:
        stage = Path(tmp) / 'facts'
        selected, filtering = select_sources(source, settings['prefilter'], settings['exclude_modules'])
        log.with_suffix('.prefilter.json').write_text(json.dumps(filtering, indent=2) + '\n')
        if not selected:
            return dict(record, status='no_sources_selected')
        allowlist = Path(tmp) / 'sources.txt'
        allowlist.write_text('\n'.join(selected) + '\n')
        environment = dict(os.environ, CONFL_SOURCE_ALLOWLIST=str(allowlist))
        with log.open('w') as stream:
            proc = subprocess.run(['java', *settings['java_options'], '-jar', str(jar), case_id, str(source), str(stage)],
                                  stdout=stream, stderr=subprocess.STDOUT, env=environment)
        if proc.returncode == 0 and 'Source prefilter:' in log.read_text(errors='replace') and valid_facts(stage):
            (stage / 'source-prefilter.json').write_text(json.dumps(filtering, indent=2) + '\n')
            stage.rename(facts)
            return dict(record, status='generated')
        return dict(record, status='extraction_error', exit_code=proc.returncode, log=str(log))


def ensure_facts(source, facts, settings, log, case_id='case'):
    """Reuse valid facts or extract them; raise ValueError when facts cannot be produced."""
    facts = Path(facts)
    if valid_facts(facts):
        return dict(case_id=case_id, facts=str(facts), status='skipped_valid')
    if facts.exists():
        raise ValueError(f'Existing facts at {facts} failed validation; remove the directory and retry')
    record = extract(source, facts, settings, log, case_id)
    if record['status'] != 'generated':
        raise ValueError(f"Facts extraction {record['status']} for {case_id}; see {log}")
    return record


def extract_dataset(cases, settings, log_dir, jobs=1, progress=print):
    """cases: (case_id, source, facts) triples. Each case runs in its own JVM; valid facts are skipped."""
    def process(case_id, source, facts):
        if valid_facts(facts):
            return dict(case_id=case_id, facts=str(facts), status='skipped_valid')
        if Path(facts).exists():
            return dict(case_id=case_id, facts=str(facts), status='invalid_existing',
                        error='Existing facts failed validation; remove the directory and retry')
        if not Path(source).is_dir():
            return dict(case_id=case_id, source=str(source), status='missing_source')
        try:
            return extract(source, facts, settings, Path(log_dir) / (case_id + '.log'), case_id)
        except OSError as exc:
            return dict(case_id=case_id, status='extraction_error', error=str(exc))
    results = []
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = [pool.submit(process, *case) for case in cases]
        for future in as_completed(futures):
            results.append(future.result())
            progress(f"[{len(results)}/{len(cases)}] {results[-1]['case_id']}: {results[-1]['status']}")
    return sorted(results, key=lambda r: r['case_id'])
