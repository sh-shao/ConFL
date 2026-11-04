"""Source comparison before accepting a ranking; no evaluation labels."""
def source_lines(kb, method):
    try:
        return len(kb.get_code_snippet(method)['lines'])
    except (OSError, ValueError, KeyError):
        return 0


def dispatch_evidence(kb, proposed, report, depth=3):
    """Overrides that run instead of the proposed method (or a parent method it calls) when the
    receiver is a subclass the report points at; plus thin factory/accessor/wrapper bodies."""
    if not hasattr(kb, 'overrides'):
        return {}
    related = kb.report_types(report)
    runs_instead = [dict(override=m, of=proposed, report_evidence=related[m.rsplit('#', 1)[0]])
                    for m in kb.overrides(proposed) if m.rsplit('#', 1)[0] in related]
    # Calls on the same object (its own class hierarchy), followed a few levels: tryLock -> tryAcquire -> tryLockInnerAsync.
    family = kb.type_family(proposed.rsplit('#', 1)[0])
    seen, frontier = {proposed}, [proposed]
    for _ in range(depth):
        following = []
        for caller in frontier:
            for callee in kb.callees(caller):
                if callee in seen or callee.rsplit('#', 1)[0] not in family:
                    continue
                seen.add(callee)
                following.append(callee)
                runs_instead += [dict(override=m, of=callee, called_from=caller, report_evidence=related[m.rsplit('#', 1)[0]])
                                 for m in kb.overrides(callee) if m.rsplit('#', 1)[0] in related]
        frontier = following
    # Being a subtype of a named supertype does not identify the receiver; it would pick an arbitrary sibling.
    runs_instead = [d for d in runs_instead if not d['report_evidence'].startswith('subtype of')]
    runs_instead = list({d['override']: d for d in reversed(runs_instead)}.values())[::-1]
    # Overrides with real logic first: not a thin body, then longer source.
    runs_instead.sort(key=lambda d: (kb.body_kind(d['override']) is not None, -source_lines(kb, d['override'])))
    runs_instead = runs_instead[:8]
    out = dict(runtime_dispatch=runs_instead) if runs_instead else {}
    kind = kb.body_kind(proposed)
    if kind:
        delegates = kb.callees(proposed)
        for cls in kb.constructs(proposed):
            delegates += [m for m in kb.methods_of(cls) if '#<' not in m]
        out.update(body_kind=kind, delegate_candidates=list(dict.fromkeys(delegates)))
    return out


def comparison_packet(kb, proposed, pool, limit=4, report=''):
    name = proposed.rsplit('#', 1)[-1].split('(')[0]
    owner = proposed.rsplit('#', 1)[0]
    pooled = {r['name'] for r in pool}
    dispatch = dispatch_evidence(kb, proposed, report)
    if 'delegate_candidates' in dispatch:
        order = {r['name']: i for i, r in enumerate(pool)}
        dispatch['delegate_candidates'] = sorted((m for m in dispatch['delegate_candidates'] if m in order), key=order.get)[:12]
    alternatives = [r['name'] for r in pool if r['name'] != proposed and
                    r['name'].rsplit('#', 1)[-1].split('(')[0] == name]
    # Overrides in the proposed class's own hierarchy (parent, subclass, implementor) come first.
    family = kb.type_family(owner) if hasattr(kb, 'type_family') else set()
    alternatives.sort(key=lambda m: (m.rsplit('#', 1)[0] not in family, m.rsplit('#', 1)[0] == owner, m))
    # Overrides that actually run for the report's receiver lead the comparison.
    running = [d['override'] for d in dispatch.get('runtime_dispatch', []) if d['override'] in pooled]
    alternatives = list(dict.fromkeys(running + alternatives))
    methods = [proposed] + alternatives[:limit-1]
    sources = []
    for method in methods:
        try:
            snippet = kb.get_code_snippet(method)
            original = snippet['lines']
            snippet['lines'] = [line[:400] for line in original[:160]]
            snippet['truncated'] = len(original) > 160 or any(len(line) > 400 for line in original[:160])
            budgeted = []
            size = 0
            for line in snippet['lines']:
                if size + len(line) > 6000:
                    snippet['truncated'] = True
                    break
                budgeted.append(line)
                size += len(line)
            snippet['lines'] = budgeted
            snippet['note'] = 'Comparison preview capped at 6000 characters per method. get_code_snippet returns the lexical method boundary with pagination.'
            sources.append(snippet)
        except (OSError, ValueError, KeyError) as exc:
            sources.append(dict(method=method, unavailable=type(exc).__name__))
    return dict(proposed=proposed, sources=sources, **dispatch,
                instruction='This is a provisional selection, not a confirmed rank. Compare the proposed method with same-name alternatives. Identify concrete implementation statements explaining the report symptom. Distinguish interface declarations, parent implementations and concrete implementations. MAY interactions and matching names alone are insufficient. runtime_dispatch lists subclass overrides that execute instead of the proposed method, or of a parent method it calls, when the receiver is that subclass; report_evidence says why the report\'s object is that subclass. Java runs the override, so when the report\'s object is that subclass, rank the override rather than the parent method. body_kind factory, accessor or wrapper means the method only creates, returns or forwards an object; the faulty logic is usually in delegate_candidates, the methods it calls or the class it creates. Confirm with a revised evidence-based explanation or select/search a better candidate. Alternatives are not automatically proven relevant.')
