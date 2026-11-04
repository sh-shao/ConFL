"""Schema-v2 facts, thread reachability and the evidence-only interaction DSL built from them."""
import csv
import hashlib
import json
from collections import defaultdict, deque
from itertools import combinations
from pathlib import Path
from .model import derive, add_client_api_entries, add_declared_entries

OPTIONAL_ARITIES = {'CandidateCall': 4, 'AccessEvidence': 4,
                    # Raw syntactic relations interpreted by model.py; absent in older facts.
                    'FunctionalArg': 6, 'ExternalSuper': 2, 'Annotation': 2, 'Modifier': 2, 'FieldInfo': 2, 'FieldCall': 5}

ARITIES = {
    'Class': 1, 'Extends': 2, 'Implements': 2, 'Method': 1, 'InClass': 2,
    'MethodSig': 3, 'DispatchMethod': 1, 'DirectCall': 2, 'VirtualCall': 4,
    'AsyncCall': 3, 'ThreadEntry': 1, 'ReentrantEntry': 1, 'EntryKind': 2,
    'Access': 4, 'Guard': 4, 'Sync': 4, 'Location': 2, 'Unresolved': 3,
}


def read_rows(path, width):
    rows = set()
    with Path(path).open(encoding='utf-8', newline='') as stream:
        # Facts contain literal backslashes and quotes, not CSV quoting.
        for i, row in enumerate(csv.reader(stream, delimiter='\t', quoting=csv.QUOTE_NONE), 1):
            if len(row) != width or any(not col for col in row):
                raise ValueError(f'{path}:{i}: expected {width} nonempty columns, got {row!r}')
            rows.add(tuple(row))
    return rows


def load_facts(directory, model=True, client_api=False, extra_entries=()):
    """Read and validate schema-v2 facts; model=True adds model.py derived semantics.

    client_api=True (requires model) also treats public methods without a modeled
    in-project caller as reentrant client entries; off by default. extra_entries are
    user-declared entry rules (model.add_declared_entries).
    """
    directory = Path(directory)
    if (directory / 'schema-version.txt').read_text(encoding='utf-8').strip() != '2':
        raise ValueError('Expected schema v2 facts; regenerate using FactsExtractor Main')
    facts = {name: read_rows(directory / f'{name}.facts', width) for name, width in ARITIES.items()}
    for name, width in OPTIONAL_ARITIES.items():
        path = directory / f'{name}.facts'
        facts[name] = read_rows(path, width) if path.exists() else set()
    methods = {r[0] for r in facts['Method']}
    for name in ('InClass', 'MethodSig', 'DispatchMethod', 'ThreadEntry', 'ReentrantEntry', 'EntryKind', 'Access', 'Guard', 'Sync', 'Location', 'DirectCall', 'VirtualCall', 'AsyncCall'):
        for row in facts[name]:
            if row[0] not in methods:
                raise ValueError(f'{name}: undeclared method {row[0]}')
    for caller, target, site, reason in facts.get('CandidateCall', set()):
        if caller not in methods or target not in methods:
            raise ValueError(f'CandidateCall: undeclared endpoint at {site}')
    for method, resource, site, reason in facts.get('AccessEvidence', set()):
        if (method, resource, 'R', site) not in facts['Access'] and (method, resource, 'RW', site) not in facts['Access']:
            raise ValueError(f'AccessEvidence: missing access at {site}')
    for caller, target, site in facts['AsyncCall']:
        if target not in methods:
            raise ValueError(f'AsyncCall: undeclared target {target} at {site}')
    for method, resource, action, site in facts['Access']:
        if action not in ('R', 'W', 'RW'):
            raise ValueError(f'Invalid access action: {action}')
    if not {r[0] for r in facts['ReentrantEntry']} <= {r[0] for r in facts['ThreadEntry']}:
        raise ValueError('ReentrantEntry must be a subset of ThreadEntry')
    classes = {r[0] for r in facts['Class']}
    for caller, body, *_ in facts['FunctionalArg']:
        if caller not in methods or (body not in methods and body not in classes):
            raise ValueError(f'FunctionalArg: undeclared endpoint {caller} -> {body}')
    for method, *_ in facts['FieldCall']:
        if method not in methods:
            raise ValueError(f'FieldCall: undeclared method {method}')
    if not model:
        if client_api or extra_entries:
            raise ValueError('client_api and extra_entries require model=True')
        return facts
    facts = derive(facts)
    if extra_entries:
        facts = add_declared_entries(facts, extra_entries)
    return add_client_api_entries(facts, call_edges(facts)) if client_api else facts


def call_edges(facts):
    """Caller -> callees under class-hierarchy MAY rules (including inherited bodies)."""
    parents = defaultdict(set)
    types = {r[0] for r in facts['Class']}
    for child, parent in facts['Extends'] | facts['Implements']:
        parents[child].add(parent)
        types.update((child, parent))
    ancestors = {}
    for cls in types:
        seen, todo = {cls}, [cls]
        while todo:
            for parent in parents[todo.pop()]:
                if parent not in seen:
                    seen.add(parent)
                    todo.append(parent)
        ancestors[cls] = seen
    methods = {r[0] for r in facts['Method']}
    sigs = {m: (name, sig) for m, name, sig in facts['MethodSig']}
    dispatch = {r[0] for r in facts['DispatchMethod']}
    members = defaultdict(set)
    for method, owner in facts['InClass']:
        if method in dispatch:
            members[owner].add(method)
    candidates = defaultdict(set)
    for actual, supers in ancestors.items():
        for owner in supers:
            for method in members[owner]:
                if method in sigs:
                    for receiver in supers:
                        candidates[(receiver, *sigs[method])].add(method)
    calls = {(a, b) for a, b in facts['DirectCall'] if a in methods and b in methods}
    calls.update((a, b) for a, b, site, reason in facts.get('CandidateCall', set()))
    for caller, recv, name, sig in facts['VirtualCall']:
        calls.update((caller, target) for target in candidates[(recv, name, sig)])
    edges = defaultdict(set)
    for caller, target in calls:
        edges[caller].add(target)
    return edges


def derive_reachability(facts, edges=None):
    """Entry -> reachable method pairs under the call_edges MAY rules (including inherited bodies)."""
    edges = call_edges(facts) if edges is None else edges
    reach = set()
    for (entry,) in facts['ThreadEntry']:
        seen, todo = {entry}, deque([entry])
        while todo:
            for target in edges[todo.popleft()]:
                if target not in seen:
                    seen.add(target)
                    todo.append(target)
        reach.update((entry, method) for method in seen)
    return reach


def build_interaction_dsl(facts, reach, include_read_only=False):
    entries = {r[0] for r in facts['ThreadEntry']}
    methods = {r[0] for r in facts['Method']}
    by_method = defaultdict(set)
    for entry, method in reach:
        if entry not in entries or method not in methods:
            raise ValueError(f'Reachability contains unknown entry or method: {(entry, method)}')
        by_method[method].add(entry)
    guards = defaultdict(set)
    for method, resource, site, lock in facts['Guard']:
        guards[(method, resource, site)].add(lock)
    resources = defaultdict(lambda: defaultdict(list))
    for method, resource, action, site in sorted(facts['Access']):
        for entry in sorted(by_method[method]):
            resources[resource][entry].append({
                'method': method, 'action': action, 'location': site,
                'guarded_by': sorted(guards[(method, resource, site)]),
            })
    reentrant = {r[0] for r in facts['ReentrantEntry']}
    interactions = []
    for resource, groups in sorted(resources.items()):
        pairs = list(combinations(sorted(groups), 2))
        pairs += [(t, t) for t in sorted(groups) if t in reentrant]
        for left, right in sorted(pairs):
            accesses = groups[left] + groups[right]
            has_write = any(a['action'] in ('W', 'RW') for a in accesses)
            if not has_write and not include_read_only:
                continue
            key = json.dumps([resource, left, right], ensure_ascii=False)
            interactions.append({
                'id': hashlib.sha256(key.encode()).hexdigest()[:20],
                'shared_resource': resource,
                'entry_pair': [left, right],
                'contexts': [
                    {'side': 1, 'entry': left, 'accesses': groups[left]},
                    {'side': 2, 'entry': right, 'accesses': groups[right]},
                ],
                'has_write': has_write,
                'same_entry_instances': left == right,
                'status': 'potential_interaction',
            })
    return interactions


def build_grouped_dsl(facts, reach, keep=None, sample=10):
    """Interactions over entry groups instead of entry pairs (see entry_groups); lossless when keep is None.

    keep restricts accessors (e.g. a ranked budget); constructor bodies always count because
    their writes decide eligibility. Same eligibility as build_interaction_dsl: distinct
    entries, at least one writing side, self pairs only through two entries sharing a group.
    """
    by_method = defaultdict(set)
    for entry, method in reach:
        by_method[method].add(entry)
    guards = defaultdict(set)
    for method, resource, site, lock in facts['Guard']:
        guards[(method, resource, site)].add(lock)
    sites = defaultdict(list)
    accesses = [a for a in facts['Access'] if keep is None or a[0] in keep or is_construction(a[0])]
    per_entry = defaultdict(lambda: defaultdict(set))
    for method, resource, action, site in sorted(accesses):
        sites[(method, resource, action)].append(dict(location=site, guarded_by=sorted(guards[(method, resource, site)])))
        for entry in by_method[method]:
            per_entry[resource][entry].add((method, action))
    interactions = []
    for resource, entries in sorted(per_entry.items()):
        groups = defaultdict(list)
        for entry, key in entries.items():
            groups[frozenset(key)].append(entry)
        keys = sorted(groups, key=lambda k: sorted(groups[k])[0])
        def context(side, key):
            members = sorted(groups[key])
            return dict(side=side, entry_count=len(members), entries=members[:sample],
                        accesses=[dict(method=m, action=a, sites=sites[(m, resource, a)]) for m, a in sorted(key)])
        for i, left in enumerate(keys):
            for right in keys[i:]:
                if left == right and len(groups[left]) < 2:
                    continue
                if not any(a != 'R' for m, a in left | right):
                    continue
                if keep is not None and not any(m in keep for m, a in left | right):
                    continue  # constructor-only on both sides: no kept method involved
                key = json.dumps([resource, sorted(map(list, left)), sorted(map(list, right))], ensure_ascii=False)
                interactions.append(dict(id=hashlib.sha256(key.encode()).hexdigest()[:20], shared_resource=resource,
                                         contexts=[context(1, left), context(2, right)],
                                         entry_pairs=len(groups[left]) * len(groups[right]) if left != right else
                                         len(groups[left]) * (len(groups[left]) - 1) // 2,
                                         same_group=left == right, status='potential_interaction'))
    return interactions


def method_evidence(facts, reach, construction_writes=True, context_depth=2, edges=None, construction_members=False):
    """Aggregate distinct-entry shared-write evidence per method without materializing pairs.

    Returns (methods, stats). methods maps method -> dict(score, tier, role, resources);
    the evaluator ranks on (tier, -score). Same eligibility as build_interaction_dsl in
    paper mode: two distinct entries, at least one writing side. Self pairs excluded.
    Reachable callers up to context_depth calls above an accessor are 'context' methods
    (tier 2): they lead a thread to the shared access but do not touch it themselves.
    Constructor/initializer bodies still make resources eligible (their writes count) and
    still lead to context callers, but are not members unless construction_members: an
    object under construction is not yet shared.
    """
    by_method = defaultdict(set)
    for entry, method in reach:
        by_method[method].add(entry)
    entries, writers = defaultdict(set), defaultdict(set)
    for method, resource, action, site in facts['Access']:
        if not construction_writes and is_construction(method):
            continue
        entries[resource].update(by_method[method])
        if action != 'R':
            writers[resource].update(by_method[method])
    eligible = {r for r, es in entries.items() if len(es) >= 2 and writers[r]}
    methods = defaultdict(lambda: dict(score=0, tier=1, role='accessor', resources=set()))
    for method, resource, action, site in facts['Access']:
        if resource in eligible and by_method[method] and (construction_writes or not is_construction(method)):
            methods[method]['resources'].add(resource)
    methods = dict(methods)
    for row in methods.values():
        row['score'] = len(row['resources'])
    if context_depth:
        callers = defaultdict(set)
        for caller, targets in (call_edges(facts) if edges is None else edges).items():
            for target in targets:
                callers[target].add(caller)
        frontier = set(methods)
        for depth in range(1, context_depth + 1):
            found = {}
            for method in frontier:
                for caller in callers[method]:
                    # Reachable callers only; an accessor or a nearer context keeps its own role.
                    if by_method[caller] and (caller not in methods or caller in found):
                        found.setdefault(caller, set()).add(method)
            for caller, via in found.items():
                methods[caller] = dict(score=len(via), tier=1 + depth, role='context', resources=set(), via=via)
            frontier = set(found)
    if not construction_members:
        methods = {m: row for m, row in methods.items() if not is_construction(m)}
    for row in methods.values():
        row['resources'] = sorted(row['resources'])
        if 'via' in row:
            row['via'] = sorted(row['via'])
    def pairs(n):
        return n * (n - 1) // 2
    interactions = sum(pairs(len(entries[r])) - pairs(len(entries[r] - writers[r])) for r in eligible)
    groups = entry_groups(facts, by_method, eligible, construction_writes)
    grouped = 0
    for r, keyed in groups.items():
        writing = [has_write for has_write, size in keyed.values()]
        grouped += pairs(len(writing)) - pairs(writing.count(False)) + sum(w and n >= 2 for w, n in keyed.values())
    stats = dict(entries=len({r[0] for r in facts['ThreadEntry']}), reach_pairs=len(reach), reachable_methods=len(by_method),
                 resources=len(eligible), interactions=interactions, grouped_interactions=grouped, dsl_methods=len(methods),
                 accessor_methods=sum(r['role'] == 'accessor' for r in methods.values()),
                 context_methods=sum(r['role'] == 'context' for r in methods.values()))
    return dict(methods), stats


def entry_groups(facts, by_method, resources, construction_writes=True):
    """Per resource, entries keyed by the exact set of (accessor, action) they reach on it.

    Entries with the same key produce identical interaction contexts, so a pair of keys (or a
    key shared by two or more entries) stands for every entry pair it covers: lossless.
    Returns {resource: {key: (has_write, entry_count)}}.
    """
    reached = defaultdict(lambda: defaultdict(set))
    for method, resource, action, site in facts['Access']:
        if resource in resources and (construction_writes or not is_construction(method)):
            for entry in by_method[method]:
                reached[resource][entry].add((method, action))
    out = {}
    for resource, per_entry in reached.items():
        keyed = defaultdict(int)
        for accesses in per_entry.values():
            keyed[frozenset(accesses)] += 1
        out[resource] = {key: (any(a != 'R' for m, a in key), n) for key, n in keyed.items()}
    return out


def is_construction(method):
    name = method.rsplit('#', 1)[-1]
    return name.startswith(('<init>(', '<clinit>(', '<instance-init>('))


def build_document(facts, reach, include_read_only=False, interactions=None):
    return {
        'schema_version': 2,
        'analysis': 'source-level class-based may analysis',
        'limitations': [
            'Interactions are candidates, not proven races or happens-before results.',
            'Field identities merge object instances; aliases are not tracked.',
            'Self pairs denote possible concurrent invocations, not proven simultaneous execution.',
            'Guards describe lexical synchronized scopes; explicit lock events are not proven guards.',
            'Unresolved and unsupported constructs are reported in diagnostics.',
            'Reachability may include uncertain source candidates listed in candidate_calls.',
        ],
        'interactions': build_interaction_dsl(facts, reach, include_read_only) if interactions is None else interactions,
        'async_edges': [dict(zip(('caller', 'target', 'location'), row)) for row in sorted(facts['AsyncCall'])],
        'synchronization': [dict(zip(('method', 'resource', 'action', 'location'), row)) for row in sorted(facts['Sync'])],
        'candidate_calls': [dict(zip(('caller', 'target', 'location', 'reason'), row)) for row in sorted(facts.get('CandidateCall', set()))],
        'access_evidence': [dict(zip(('method', 'resource', 'location', 'reason'), row)) for row in sorted(facts.get('AccessEvidence', set()))],
        'entry_kinds': [dict(zip(('entry', 'kind'), row)) for row in sorted(facts['EntryKind'])],
        'diagnostics': [dict(zip(('location', 'kind', 'detail'), row)) for row in sorted(facts['Unresolved'])],
    }

