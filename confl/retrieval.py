"""Bounded, report-guided retrieval. Never reads evaluation labels."""
import re
import math
from collections import Counter


REDIRECT_DEPTH = 300  # declaration redirect is checked on the best-scored rows only
# Report anchors within ANCHOR_HOPS calls take one of every ANCHOR_EVERY shortlist slots (dev-tuned:
# bug_hit@120 81.6->83.9%, holdout 72.1->73.5%; tools/report_anchor_recall.py).
ANCHOR_HOPS, ANCHOR_EVERY = 1, 4
INERT_FILTER = True
THIN_BODY_PENALTY = 15  # dev-tuned (druid, grpc, redisson): 0->15 raises shortlist recall@120 41.2->47.6%


def same_method(a, b):
    """Same class and simple method name (overloads)."""
    return a.split('(')[0] == b.split('(')[0]


def arity(method):
    params = method.split('(', 1)[1].rstrip(')')
    return params.count(',') + 1 if params else 0


def retrieve_methods(kb, report, component, interactions, limit=120, anchors=None):
    retained = {m['name'] for m in kb.list_modules()}
    dsl = {a['method'] for i in interactions for c in i['contexts'] for a in c['accesses']}
    identifiers = set(re.findall(r'[A-Za-z_$][\w$]*', report))
    reported_classes = {owner for owner in kb.owners.values()
                        if owner.rsplit('.', 1)[-1] in identifiers}
    # Implementations of report-named interfaces/classes get partial credit (generic dispatch semantics).
    subtypes = getattr(kb, 'subtypes', None)
    subtype_classes = set().union(*(subtypes(c) for c in reported_classes)) - reported_classes if subtypes and reported_classes else set()
    pool = {m for m, meta in kb.meta.items() if meta['component'] in retained}
    def terms(text):
        text = re.sub(r'([a-z])([A-Z])', r'\1 \2', text)
        return {w.lower() for w in re.findall(r'[A-Za-z]+', text) if len(w) > 2}
    report_terms = terms(report)
    documents = {m: terms(kb.owners.get(m, '') + ' ' + m.split('#')[-1].split('(')[0]) for m in pool}
    frequencies = Counter(t for doc in documents.values() for t in doc)
    # Optional bug-agnostic concurrency prior from the ranked DSL: top rank ~weight, tail ~0.
    weight = getattr(kb, 'dsl_prior_weight', 0.0)
    ranks = kb.suspicion_ranks() if weight else {}
    scale = math.log1p(len(ranks)) if ranks else 1.0
    rows = []
    for m in pool:
        owner = kb.owners.get(m, '')
        method = m.rsplit('#', 1)[-1].split('(')[0]
        named_class = owner in reported_classes
        named_method = method in identifiers and len(method) > 3
        subtype_class = owner in subtype_classes
        lexical = sum(math.log(1 + len(pool)/(1 + frequencies[t])) for t in documents[m] & report_terms)
        prior = weight * (1 - math.log1p(ranks[m]) / scale) if m in ranks else 0.0
        score = lexical + 10 * named_class + 4 * subtype_class + 6 * named_method + 2 * (kb.meta[m]['component'] == component) + (m in dsl) + prior
        rows.append(dict(name=m, component=kb.meta[m]['component'], score=score, evidence_sources=[tag for tag, ok in
                    [('selected_pair_dsl', m in dsl), ('selected_component', kb.meta[m]['component'] == component),
                     ('report_class', named_class), ('report_class_subtype', subtype_class),
                     ('report_method_name', named_method), ('dsl_suspicion', prior > 0),
                     ('report_anchor', anchors is not None and anchors.get(m) == 0),
                     ('near_report_anchor', anchors is not None and 0 < anchors.get(m, ANCHOR_HOPS + 1) <= ANCHOR_HOPS)] if ok]))
    rows.sort(key=lambda r: (-r['score'], r['name']))
    # A body-less declaration has no fault-bearing code: hand its score to the overriding
    # implementations (same name and arity in a subtype) and demote it below them.
    is_declaration = getattr(kb, 'is_declaration', None)
    if is_declaration and subtypes:
        by_name = {}
        for row in rows:
            by_name.setdefault(row['name'].rsplit('#', 1)[-1].split('(')[0], []).append(row)
        for row in rows[:REDIRECT_DEPTH]:
            if not is_declaration(row['name']):
                continue
            owner, method = row['name'].rsplit('#', 1)
            below = subtypes(owner)
            for other in by_name[method.split('(')[0]]:
                if other['name'].rsplit('#', 1)[0] in below and arity(other['name']) == arity(row['name']) \
                        and not is_declaration(other['name']) and other['score'] < row['score'] - 0.5:
                    other['score'] = row['score'] - 0.5
                    other['evidence_sources'].append('implements_ranked_declaration')
            row['score'] -= 10
            row['evidence_sources'].append('declaration_only')
        rows.sort(key=lambda r: (-r['score'], r['name']))
    # Factories, accessors and one-line forwarders rarely hold the faulty logic; let the code they reach rank first.
    body_kind = getattr(kb, 'body_kind', None)
    if body_kind and THIN_BODY_PENALTY:
        for row in rows[:REDIRECT_DEPTH]:
            kind = body_kind(row['name'])
            if kind:
                row['score'] -= THIN_BODY_PENALTY
                row['evidence_sources'].append('thin_body_' + kind)
        rows.sort(key=lambda r: (-r['score'], r['name']))
    # Declarations and short methods with no state effect leave the shortlist (still searchable in the pool).
    is_inert = getattr(kb, 'is_inert', None)
    if is_inert and INERT_FILTER:
        for row in rows[:REDIRECT_DEPTH]:
            if 'declaration_only' in row['evidence_sources'] or is_inert(row['name']):
                row['evidence_sources'].append('inert')
    # Round-robin per class prevents a large class monopolizing the shortlist.
    # One shortlist slot per overload family; extra overloads only fill leftover space.
    selected, deferred, counts, families, overloads = [], [], Counter(), set(), []
    for row in rows:
        owner = row['name'].rsplit('#', 1)[0]
        family = row['name'].split('(')[0]
        if family in families:
            overloads.append(row)
        elif counts[owner] < 8:
            families.add(family)
            selected.append(row)
            counts[owner] += 1
        else:
            deferred.append(row)
    selected = selected + deferred + overloads
    if is_inert and INERT_FILTER:
        selected = [r for r in selected if 'inert' not in r['evidence_sources']] + [r for r in selected if 'inert' in r['evidence_sources']]
    if anchors:
        # Anchors (stack frames, quoted messages, report calls; see report_anchors.py) nearest first,
        # interleaved so they reach the top without displacing the lexical order wholesale.
        by_name = {r['name']: r for r in rows}
        near = sorted((m for m, d in anchors.items() if d <= ANCHOR_HOPS and m in by_name), key=lambda m: (anchors[m], -by_name[m]['score'], m))
        sources = (iter([by_name[m] for m in near]), iter(selected))
        merged, seen, took = [], set(), True
        while took:  # one anchor, then ANCHOR_EVERY-1 lexical rows, until both run out
            took = False
            for source, quota in zip(sources, (1, ANCHOR_EVERY - 1)):
                for _ in range(quota):
                    row = next((r for r in source if r['name'] not in seen), None)
                    if row is not None:
                        merged.append(row)
                        seen.add(row['name'])
                        took = True
        selected = merged
    selected = selected[:limit]
    return selected, dict(pool_size=len(rows), shortlist_size=min(limit, len(rows)),
                             limit=limit, omitted_count=max(0, len(rows)-limit), candidates=rows,
                             policy='Full retained method pool; initial report-guided shortlist, searchable on demand. Filtered modules remain excluded.')


def method_interactions(interactions, method_name):
    return [dict(shared_resource=i['shared_resource'], entry_pair=i.get('entry_pair'),
                 contexts=[dict(entry=c.get('entry'), accesses=[a for a in c['accesses'] if a['method'] == method_name])
                           for c in i['contexts']])
            for i in interactions if any(a['method'] == method_name for c in i['contexts'] for a in c['accesses'])]


def search_methods(rows, query, excluded=(), limit=20):
    words = set(re.findall(r'[A-Za-z_$][\w$]*', query.lower()))
    if not words:
        raise ValueError('Provide a class, package, method or component search term')
    scored = []
    for row in rows:
        if row['name'] in excluded:
            continue
        text = (row['name'] + ' ' + row.get('component', '')).lower()
        score = sum(word in text for word in words)
        if score:
            scored.append((score, row))
    scored.sort(key=lambda pair: (-pair[0], -pair[1]['score'], pair[1]['name']))
    return dict(matches=[row for _, row in scored[:limit]], total_matches=len(scored),
                note='Matches become selectable candidates. Refine query if truncated. Excluded modules remain inaccessible.')
