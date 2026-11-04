"""Bug-agnostic concurrency-suspicion ranking for DSL methods.

method_evidence decides membership (coverage); this module only orders members, so a
budgeted prefix never changes which methods are eligible. Signals are generic Java
concurrency semantics (guards, volatile/atomic state, sync ops, entry kinds, object
escape, class concurrency density), never project- or GT-specific. Weights were set on
the dev projects only.
"""
import math
import re
from collections import defaultdict, deque

from .dsl import is_construction, call_edges, method_evidence

# Entries from naming guesses or the open-world API model are weaker than real thread
# starts, executor submissions and async/framework callbacks.
WEAK_ENTRY_KINDS = {'client-api', 'main', 'task-candidate'}
LIFECYCLE = re.compile(r'^(close|stop|shutdown|start|run|cancel|release|acquire|destroy|dispose|terminate|interrupt|'
                       r'complete|fail|abort|reset|clear|flush|init\w*|open|connect|disconnect|reconnect|register|unregister|'
                       r'subscribe|unsubscribe|add\w*|remove\w*|put\w*|set\w*|update\w*|on[A-Z]\w*|handle\w*|process\w*|'
                       r'schedule\w*|submit\w*|execute\w*|poll\w*|offer\w*|take\w*|await\w*|signal\w*|notify\w*|wait\w*|'
                       r'lock\w*|unlock\w*|retain|refresh\w*|evict\w*|expire\w*|timeout\w*|renew\w*|check\w*|ensure\w*)$')
SYNC_OPS = {'lock', 'unlock', 'tryLock', 'lockInterruptibly', 'wait', 'notify', 'notifyAll', 'await', 'signal', 'signalAll',
            'acquire', 'release', 'tryAcquire', 'countDown', 'compareAndSet', 'getAndSet', 'incrementAndGet', 'decrementAndGet',
            'getAndIncrement', 'getAndDecrement', 'addAndGet', 'getAndAdd', 'lazySet', 'updateAndGet', 'getAndUpdate',
            'accumulateAndGet', 'complete', 'completeExceptionally', 'cancel', 'join', 'park', 'unpark'}
NOISE_FIELD = re.compile(r'^(log|LOG|logger|LOGGER|_log|_logger|serialVersionUID|metrics?|stats|config|conf|configuration|'
                         r'jsonMapper|objectMapper|mapper|emitter|clock|ticker)$')
CONCURRENT_TYPE = re.compile(r'Atomic|Concurrent|Lock|Semaphore|Latch|Future|Promise|Executor|BlockingQueue|Condition|Barrier|Phaser')
CONFINED_TYPE = re.compile(r'(Iterator|Builder|Spliterator|Visitor|Parser|Reader|Writer|Encoder|Decoder|Formatter)$')

# Resource quality: how plausible a harmful shared access is (log-scaled counts).
# Coordinate search on dev (objective R@100+R@200+R@500); leave-one-project-out kept R@200 level, R@500 +3 points.
RESOURCE_WEIGHTS = dict(base=0.5, unguarded=0.0, mixed=1.45, volatile=0.3, state=0.4, strong_multi=0.3, strong_writer=0.6,
                        escapes=0.6, density=1.1, popularity=-0.1, confined=-1.2, noise=-2.5)
METHOD_WEIGHTS = dict(best=1.0, rest=0.35, writes=0.5, unguarded_write=0.5, sync_ops=1.1, synchronized=0.0, density=0.35,
                      strong_entries=0.25, lifecycle=0.5, distance=-0.12, construction=-3.5, context=0.6, context_tier=-0.55,
                      via=0.3)


def log1(x):
    return math.log1p(max(x, 0))


def field_of(resource):
    parts = resource.split('#')
    return '#'.join(parts[:2]), ('#' + parts[2] if len(parts) > 2 else '')


def owner(method):
    return method.rsplit('#', 1)[0]


def name_of(method):
    return method.rsplit('#', 1)[-1].split('(')[0]


def class_ancestors(facts):
    parents = defaultdict(set)
    for child, parent in facts['Extends'] | facts['Implements'] | facts.get('ExternalSuper', set()):
        parents[child].add(parent.lstrip('?'))
    memo = {}
    def walk(cls):
        if cls not in memo:
            memo[cls] = {cls}
            for p in parents[cls]:
                memo[cls] |= walk(p)
        return memo[cls]
    return walk


def method_features(facts, reach, evidence, edges):
    """Per DSL method: scalar features plus a list of per-resource flag dicts ('res')."""
    by_method = defaultdict(set)
    for entry, method in reach:
        by_method[method].add(entry)
    kinds = defaultdict(set)
    for entry, kind in facts['EntryKind']:
        kinds[entry].add(kind)
    entries = {e for (e,) in facts['ThreadEntry']}
    strong = {e for e in entries if not kinds[e] or kinds[e] - WEAK_ENTRY_KINDS}
    modifiers = defaultdict(set)
    for element, mod in facts.get('Modifier', ()):
        modifiers[element].add(mod)
    guarded = {(m, r, s) for m, r, s, lock in facts['Guard']}
    sync_ops = defaultdict(int)
    for method, resource, op, site in facts['Sync']:
        sync_ops[method] += op in SYNC_OPS
    field_types = dict(facts.get('FieldInfo', ()))
    # Escape: a class's instances can be shared if some field may hold them (declared type is
    # an ancestor), or its methods are thread entries (the task object itself is handed over).
    ancestors = class_ancestors(facts)
    held = {t.lstrip('?') for t in field_types.values()}
    held_simple = {t.rsplit('.', 1)[-1] for t in held}
    entry_classes = {owner(e) for e in entries}
    escape_memo = {}
    def escapes(cls):
        if cls not in escape_memo:
            outer = cls.split('$')[0]
            escape_memo[cls] = cls in entry_classes or outer in entry_classes or any(
                a in held or a.rsplit('.', 1)[-1] in held_simple for a in ancestors(cls))
        return escape_memo[cls]

    accessors, guard_mix, construction_only = defaultdict(set), defaultdict(lambda: [0, 0]), defaultdict(lambda: True)
    res_entries, res_strong_writers = defaultdict(set), defaultdict(set)
    for method, resource, action, site in facts['Access']:
        if not by_method[method]:
            continue
        accessors[resource].add(method)
        guard_mix[resource][(method, resource, site) in guarded] += 1
        res_entries[resource] |= by_method[method]
        if action != 'R':
            res_strong_writers[resource] |= by_method[method] & strong
            if not is_construction(method):
                construction_only[resource] = False
    density = defaultdict(int)
    for element, mods in modifiers.items():
        if 'volatile' in mods or 'synchronized' in mods:
            density[owner(element)] += 1
    for method, count in sync_ops.items():
        density[owner(method)] += count
    for method, resource, site in guarded:
        density[owner(method)] += 1
    for field, ftype in field_types.items():
        if CONCURRENT_TYPE.search(ftype):
            density[owner(field)] += 1
    distance = {}
    for seeds in (strong, entries):
        todo = deque(e for e in seeds if e not in distance)
        distance.update((e, 0) for e in todo)
        while todo:
            m = todo.popleft()
            for t in edges.get(m, ()):
                if t not in distance:
                    distance[t] = distance[m] + 1
                    todo.append(t)

    def resource_flags(resource):
        field, suffix = field_of(resource)
        cls = owner(field)
        mods = modifiers.get(field, set())
        g, u = guard_mix[resource][1], guard_mix[resource][0]
        static = 'static' in mods
        return dict(
            noise=(not suffix and ('final' in mods or bool(NOISE_FIELD.match(field.rsplit('#', 1)[-1])))) or construction_only[resource],
            volatile='volatile' in mods, state=bool(suffix), mixed=g > 0 and u > 0, unguarded=g == 0,
            strong_writer=len(res_strong_writers[resource]), strong_multi=len(res_entries[resource] & strong),
            escapes=static or escapes(cls), confined=not static and bool(CONFINED_TYPE.search(cls.split('$')[0])),
            popularity=len(accessors[resource]), density=density[cls],
        )
    flags = {}
    accesses = defaultdict(list)
    for method, resource, action, site in facts['Access']:
        if method in evidence:
            accesses[method].append((resource, action, site))
    out = {}
    for method, row in evidence.items():
        resources = set(row['resources'])
        for r in resources:
            if r not in flags:
                flags[r] = resource_flags(r)
        mine = [a for a in accesses[method] if a[0] in resources]
        writes = {r for r, a, s in mine if a != 'R'}
        mods = modifiers.get(method, set())
        out[method] = dict(
            tier=row['tier'], context=row['role'] == 'context',
            res=[dict(flags[r], write=r in writes) for r in sorted(resources)],
            unguarded_write=any(a != 'R' and not flags[r]['noise'] and (method, r, s) not in guarded for r, a, s in mine),
            construction=is_construction(method), synchronized='synchronized' in mods,
            sync_ops=sync_ops[method], density=density[owner(method)],
            entries=len(by_method[method]), strong_entries=len(by_method[method] & strong),
            distance=distance.get(method, 99), lifecycle=bool(LIFECYCLE.match(name_of(method))),
            via=sorted(row.get('via', ())),
        )
    return out


def resource_score(f, w=RESOURCE_WEIGHTS):
    s = w['base'] + w['unguarded'] * f['unguarded'] + w['mixed'] * f['mixed'] + w['volatile'] * f['volatile']
    s += w['state'] * f['state'] + w['strong_multi'] * log1(f['strong_multi']) + w['strong_writer'] * log1(f['strong_writer'])
    s += w['escapes'] * f['escapes'] + w['density'] * log1(f['density']) + w['popularity'] * log1(f['popularity'] / 10)
    s += w['confined'] * f['confined'] + w['noise'] * f['noise']
    return s


def score_methods(features, rw=RESOURCE_WEIGHTS, mw=METHOD_WEIGHTS):
    """Accessors: best resource + diminishing rest + method signals. Context: discounted best accessor."""
    scores = {}
    for method, f in features.items():
        if f['context']:
            continue
        quality = sorted((resource_score(r, rw) + (0.5 * r['write']) for r in f['res']), reverse=True)
        s = mw['best'] * (quality[0] if quality else 0) + mw['rest'] * sum(max(q, 0) for q in quality[1:4])
        s += mw['writes'] * log1(sum(r['write'] and not r['noise'] for r in f['res'])) + mw['unguarded_write'] * f['unguarded_write']
        s += mw['sync_ops'] * log1(f['sync_ops']) + mw['synchronized'] * f['synchronized'] + mw['density'] * log1(f['density'])
        s += mw['strong_entries'] * log1(f['strong_entries']) + mw['lifecycle'] * f['lifecycle']
        s += mw['distance'] * min(f['distance'], 10) + mw['construction'] * f['construction']
        scores[method] = s
    # Context methods by increasing tier so a tier-3 caller can see its tier-2 callee's score.
    for method in sorted((m for m, f in features.items() if f['context']), key=lambda m: features[m]['tier']):
        f = features[method]
        best = max((scores.get(v, 0) for v in f['via']), default=0)
        scores[method] = mw['context'] * best + mw['context_tier'] * (f['tier'] - 1) + mw['via'] * log1(len(f['via'])) \
            + mw['sync_ops'] * log1(f['sync_ops']) + mw['density'] * log1(f['density']) + mw['lifecycle'] * f['lifecycle'] \
            + mw['distance'] * min(f['distance'], 10)
    return scores


def rank(features, scores=None):
    scores = score_methods(features) if scores is None else scores
    return sorted(features, key=lambda m: (-scores[m], m))


def ranked_members(facts, reach, budget=None):
    """DSL methods in suspicion order, optionally cut to the first budget."""
    edges = call_edges(facts)
    evidence, stats = method_evidence(facts, reach, edges=edges)
    order = rank(method_features(facts, reach, evidence, edges))
    return order if budget is None else order[:budget]
