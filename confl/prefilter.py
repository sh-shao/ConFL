"""Cheap, conservative lexical module selection; not a concurrency proof."""
import os
from pathlib import Path
import re

EXCLUDE = {'test', 'tests', 'testing', 'target', '.git', 'example', 'examples', 'benchmark', 'benchmarks'}
SIGNALS = re.compile(r'\b(?:synchronized|volatile|Thread|Runnable|Callable|Executor\w*|ForkJoin\w*|CompletableFuture|FutureTask|Lock|ReentrantLock|ReadWriteLock|Semaphore|CountDownLatch|CyclicBarrier|Phaser|Atomic\w*|Concurrent\w*|Async|Scheduled)\b|java\.util\.concurrent|\.(?:submit|execute|schedule|start|wait|notify|notifyAll|parallelStream)\s*\(')


def normalize_exclusions(values):
    if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
        raise ValueError('exclude_modules must be a list of relative module paths')
    result = set()
    for value in values:
        name = value.strip().replace('\\', '/').rstrip('/')
        if not name or name.startswith('/') or any(p in ('', '.', '..') for p in name.split('/')) or any(c in name for c in '*?[]:'):
            raise ValueError('exclude_modules entries must be relative paths without wildcards or parent traversal')
        result.add(name)
    return sorted(result)


def select_sources(root, mode='concurrency_modules', exclude_modules=None):
    if mode not in ('off', 'exclude_only', 'concurrency_modules'):
        raise ValueError('prefilter must be off, exclude_only or concurrency_modules')
    root = Path(root)
    modules = {}
    exclusions = normalize_exclusions([] if exclude_modules is None else exclude_modules)
    skipped = set()
    for directory, dirs, files in os.walk(root):
        kept = []
        for directory_name in sorted(dirs):
            relative_dir = (Path(directory)/directory_name).relative_to(root).as_posix()
            if any(relative_dir == x or relative_dir.startswith(x + '/') for x in exclusions):
                skipped.add(relative_dir)
            else:
                kept.append(directory_name)
        dirs[:] = kept
        if mode != 'off':
            dirs[:] = sorted(d for d in dirs if d not in EXCLUDE)
        for name in sorted(files):
            if not name.endswith('.java'):
                continue
            file = Path(directory)/name
            relative = file.relative_to(root).as_posix()
            module = relative.split('/src/')[0] if '/src/' in relative else '.'
            text = file.read_text(encoding='utf-8', errors='replace')
            info = modules.setdefault(module, {'files': [], 'tokens': set(), 'types': set(), 'packages': set(), 'imports': set(), 'signals': set()})
            info['files'].append(relative)
            info['tokens'].update(re.findall(r'\b[A-Za-z_$][\w$]*\b', text))
            info['types'].add(file.stem)
            info['types'].update(re.findall(r'\b(?:class|interface|enum|record)\s+(\w+)', text))
            info['packages'].update(re.findall(r'\bpackage\s+([\w.]+)\s*;', text))
            info['imports'].update(re.findall(r'\bimport\s+(?:static\s+)?([\w.*]+)\s*;', text))
            info['signals'].update(m.group(0) for m in SIGNALS.finditer(text))
    selected = set(modules) if mode != 'concurrency_modules' else {m for m,v in modules.items() if v['signals']}
    reasons = {m: 'concurrency_signal' if mode == 'concurrency_modules' else mode for m in selected}
    # Keep full modules and transitive source dependencies, including ambiguous simple names.
    pending = list(selected)
    while pending and mode == 'concurrency_modules':
        caller = pending.pop()
        info = modules[caller]
        for module, dep in modules.items():
            if module in selected:
                continue
            wildcard = any(imp == package+'.*' for imp in info['imports'] for package in dep['packages'])
            if info['tokens'] & dep['types'] or wildcard:
                selected.add(module)
                reasons[module] = 'source_dependency_of:' + caller
                pending.append(module)
    files = sorted(f for m in selected for f in modules[m]['files'])
    report = dict(mode=mode, exclude_modules=exclusions, explicitly_excluded_directories=sorted(skipped),
                  unmatched_exclusions=[x for x in exclusions if not (root/x).is_dir()],
                  scanned_java_files=sum(len(v['files']) for v in modules.values()), selected_java_files=len(files),
                  excluded_directory_names=sorted(EXCLUDE) if mode != 'off' else [],
                  limitation='Lexical heuristic: comments/strings may retain extra modules; framework callbacks, reflection and external configuration may cause missed modules. Compare with off for recall.',
                  modules=[dict(name=m, included=m in selected, reason=reasons.get(m, 'no_signal_or_source_dependency'),
                                java_files=len(v['files']), signals=sorted(v['signals'])) for m,v in sorted(modules.items())])
    return files, report
