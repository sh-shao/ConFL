"""Concurrency knowledge base (CKB): facts, reachability, components and code lookups for one source tree."""
from collections import defaultdict
import json
from pathlib import Path
import re

from .components import build_component_record
from .dsl import load_facts, derive_reachability, build_interaction_dsl
from .rank import ranked_members
from .report_anchors import AnchorIndex
from .retrieval import arity
from .source_window import method_window

INERT_MAX_LINES = 5  # see CKB.is_inert
SOURCE_MATCH_LIMIT = 20  # search_source hits per query
ENTRY_OPTION_CHARS = 120000  # entry-stage options kept by relevance order; the rest is counted, not sent


def normalize_component_exclusions(values):
    if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
        raise ValueError('exclude_components must be a list of component names')
    normalized = set()
    for value in values:
        name = value.strip().replace('\\', '/').rstrip('/')
        if name != '.' and (not name or name.startswith('/') or any(p in ('', '.', '..') for p in name.split('/')) or any(c in name for c in '*?[]:')):
            raise ValueError('exclude_components requires relative component names without wildcards')
        normalized.add(name)
    return sorted(normalized)


def component_options(modules, parent=None):
    """Expose one directory level; preserve a module's own packages if it has children."""
    groups = defaultdict(list)
    prefix = parent + '/' if parent else ''
    evidence_keys = ('thread_entries', 'reachable_access_methods', 'synchronization_methods')
    for module in modules:
        evidence = module.get('concurrency_evidence')
        if not isinstance(evidence, dict) or not all(type(evidence.get(k, 0)) is int and evidence.get(k, 0) >= 0 for k in evidence_keys) or not any(evidence.get(k, 0) > 0 for k in evidence_keys):
            continue
        name = module['name']
        if parent is not None and name != parent and not name.startswith(prefix):
            continue
        if name == parent:
            groups[name].append(module)
        else:
            child = prefix + name[len(prefix):].split('/')[0]
            groups[child].append(module)
    result = []
    for name, members in sorted(groups.items()):
        own = next((m for m in members if m['name'] == name), None)
        branch = name != parent and any(m['name'] != name for m in members)
        item = dict(own) if own else dict(name=name, description=f'{name}: group of {len(members)} source modules', description_source='directory hierarchy')
        evidence = {k: sum(m['concurrency_evidence'].get(k, 0) for m in members) for k in evidence_keys}
        if branch:
            previews = [{'name': m['name'], 'summary': m.get('description', '')[:220]} for m in members[:6]]
            item['description'] = (f'{name}: group of {len(members)} retained modules. ' + ' '.join(p['name'] + ': ' + p['summary'] for p in previews[:2]))[:280]
            item['description_source'] = 'aggregated retained child module descriptions and facts'
            item['child_module_previews'] = previews
        item.update(has_children=branch, module_count=len(members), concurrency_evidence=evidence,
                    evidence_scope='included_modules_in_subtree' if branch else 'module')
        result.append(item)
    return result


def fit_options(options, limit=ENTRY_OPTION_CHARS):
    """Leading options (already in relevance order) that fit in limit serialized characters; at least one."""
    kept, size = [], 0
    for option in options:
        size += len(json.dumps(option, ensure_ascii=False)) + 2
        if kept and size > limit:
            break
        kept.append(option)
    return kept


class CKB:
    """Normalized facts retained once; interactions materialized for one pair only."""
    def __init__(self, facts, source, min_thread_entries=1, exclude_components=None, client_api=False, dsl_prior_weight=0.0,
                 extra_entries=()):
        if type(min_thread_entries) is not int or min_thread_entries < 0:
            raise ValueError("min_thread_entries must be a nonnegative integer")
        if type(dsl_prior_weight) not in (int, float) or dsl_prior_weight < 0:
            raise ValueError("dsl_prior_weight must be a nonnegative number")
        self.dsl_prior_weight = float(dsl_prior_weight)
        self.min_thread_entries = min_thread_entries
        self.exclude_components = normalize_component_exclusions([] if exclude_components is None else exclude_components)
        self.facts = load_facts(facts, client_api=client_api, extra_entries=extra_entries)
        # Declared entry rule -> number of methods it matched; 0 usually means a misspelled name.
        self.declared_entry_matches = {label: int(n) for label, n in self.facts.get('DeclaredEntryRule', ())}
        self.source = Path(source).resolve()
        self.reach = derive_reachability(self.facts)
        self.locations = dict(self.facts['Location'])
        self.owners = dict(self.facts['InClass'])
        # Type hierarchy; unresolved supertypes ('?Name') are keyed by simple name.
        self.children, self.parents = defaultdict(set), defaultdict(set)
        for child, parent in self.facts['Extends'] | self.facts['Implements']:
            key = '?' + parent[1:].rsplit('.', 1)[-1] if parent.startswith('?') else parent
            self.children[key].add(child)
            self.parents[child].add(parent.lstrip('?'))
        self._declarations, self._texts = {}, {}
        self.entries = {x[0] for x in self.facts['ThreadEntry']}
        self.methods = {x[0] for x in self.facts['Method']}
        self.reached = defaultdict(set)
        self.accesses = defaultdict(list)
        self.resources = defaultdict(set)
        self.writes = defaultdict(set)
        for m, resource, action, site in sorted(self.facts['Access']):
            self.accesses[m].append(dict(resource=resource, action=action, location=site))
        for e, m in self.reach:
            self.reached[e].add(m)
            for a in self.accesses[m]:
                self.resources[e].add(a['resource'])
                if a['action'] != 'R':
                    self.writes[e].add(a['resource'])
        self.meta = {}
        packages = {}
        for m in sorted(self.methods):
            location = self.locations.get(m, '')
            file = location.rsplit(':', 2)[0]
            component = file.split('/src/')[0] if '/src/' in file else '.'
            if file not in packages:
                path = (self.source / file).resolve()
                if not path.is_relative_to(self.source):
                    raise ValueError('Source path escapes root')
                match = re.search(r'^\s*package\s+([\w.]+)\s*;', path.read_text(encoding='utf-8'), re.M) if path.is_file() else None
                packages[file] = match.group(1) if match else '<default>'
            package = packages[file]
            self.meta[m] = dict(component=component, package=package, file=file)

    def module_filter_report(self):
        """Evidence-based candidate filter, not a proof that excluded code is sequential."""
        excluded_parts = {'benchmark', 'benchmarks', 'example', 'examples'}
        by_component = defaultdict(set)
        for method, meta in self.meta.items():
            by_component[meta['component']].add(method)
        # Demo/benchmark entries must not be the sole evidence for production helpers.
        production_entries = {e for e in self.entries
                              if not (set(Path(self.meta[e]['component']).parts) & excluded_parts)}
        reached = {m for e, m in self.reach if e in production_entries}
        synchronization = {row[0] for row in self.facts['Sync']}
        report = []
        for component, methods in sorted(by_component.items()):
            counts = dict(thread_entries=len(methods & production_entries),
                          reachable_access_methods=sum(bool(self.accesses[m]) for m in methods & reached),
                          synchronization_methods=len(methods & synchronization))
            excluded = bool(set(Path(component).parts) & excluded_parts)
            below_threshold = counts["thread_entries"] < self.min_thread_entries
            excluded_by = [name for name in self.exclude_components if component == name or component.startswith(name + "/")]
            keep = not excluded_by and not excluded and not below_threshold and any(counts.values())
            report.append(dict(name=component, included=keep, evidence=counts,
                               min_thread_entries=self.min_thread_entries, excluded_by=excluded_by,
                               reason='excluded_by_config' if excluded_by else 'excluded_demo_or_benchmark' if excluded else
                               ('below_min_thread_entries' if below_threshold else
                                ('concurrency_evidence' if keep else 'no_extracted_concurrency_evidence'))))
        return report

    def list_modules(self):
        key = (self.min_thread_entries, tuple(self.exclude_components))
        if getattr(self, '_module_cache_key', None) != key:
            self._module_cache = [build_component_record(self, record)
                                  for record in self.module_filter_report() if record['included']]
            self._module_cache_key = key
        return self._module_cache

    def list_components(self, parent=None):
        return component_options(self.list_modules(), parent)

    def list_packages_under_component(self, component_name):
        return [dict(name=p, description=p, entry_count=sum(self.meta[e]['component'] == component_name and self.meta[e]['package'] == p for e in self.entries))
                for p in sorted({v['package'] for v in self.meta.values() if v['component'] == component_name})]

    def get_component_details(self, component_name):
        if not isinstance(component_name, str) or not component_name.strip():
            raise ValueError('component_name must be a nonempty exact component path')
        modules = [m for m in self.list_modules() if m['name'] == component_name or m['name'].startswith(component_name + '/')]
        if not modules:
            raise ValueError('Unknown or filtered component')
        return dict(component=component_name, modules=modules,
                    note='Full retained module descriptions and source-backed evidence; large results are paginated.')

    def get_component_packages(self, component_name):
        """Inspect a retained component or group without committing to a package."""
        if not isinstance(component_name, str) or not component_name.strip():
            raise ValueError('component_name must be a nonempty exact component path')
        allowed = {m['name'] for m in self.list_modules()
                   if m['name'] == component_name or m['name'].startswith(component_name + '/')}
        if not allowed:
            raise ValueError('Unknown or filtered component')
        groups = defaultdict(list)
        for method, meta in self.meta.items():
            if meta['component'] in allowed:
                groups[(meta['component'], meta['package'])].append(method)
        packages = []
        for (component, package), methods in sorted(groups.items()):
            classes = sorted({self.owners[m] for m in methods if m in self.owners})
            packages.append(dict(component=component, package=package, classes=classes,
                                 class_count=len(classes), method_count=len(methods),
                                 thread_entries=sum(m in self.entries for m in methods)))
        return dict(component=component_name, packages=packages,
                    scope='retained modules only; facts-backed classes with declared methods')

    @staticmethod
    def lookup_filter_status(record):
        return dict(candidate_eligible=record['included'], filter_reason=record['reason'],
                    concurrency_evidence=record['evidence'],
                    min_thread_entries=record['min_thread_entries'], excluded_by=record['excluded_by'])

    def locate_class(self, class_name):
        if not isinstance(class_name, str) or not class_name.strip():
            raise ValueError('class_name must be a nonempty qualified or simple class name')
        query = class_name.strip()
        filters = {m['name']: m for m in self.module_filter_report()}
        locations = set()
        for method, owner in self.owners.items():
            meta = self.meta[method]
            # Prefer exact qualified identifiers; allow an unqualified class name.
            simple = owner.rsplit('.', 1)[-1].rsplit('$', 1)[-1]
            if owner == query or ('.' not in query and simple == query):
                locations.add((owner, meta['component'], meta['package'], meta['file']))
        return dict(query=query, match_policy='exact qualified name or exact simple name',
                    matches=[dict(class_name=c, component=m, package=p, file=f, **self.lookup_filter_status(filters[m]))
                             for c,m,p,f in sorted(locations)],
                    note='Searches all extracted modules with declared methods. Filtered matches are not selectable. No match does not prove absence from source.')

    def locate_package(self, package_name):
        if not isinstance(package_name, str) or not package_name.strip():
            raise ValueError('package_name must be a nonempty package name')
        query = package_name.strip()
        filters = {m['name']: m for m in self.module_filter_report()}
        locations = defaultdict(set)
        for method, meta in self.meta.items():
            if meta['package'] == query or meta['package'].startswith(query + '.'):
                locations[(meta['component'], meta['package'])].add(self.owners.get(method, ''))
        return dict(query=query, match_policy='exact package or subpackage, with dot boundary',
                    matches=[dict(component=c, package=p, classes=sorted(classes - {''}), **self.lookup_filter_status(filters[c]))
                             for (c,p),classes in sorted(locations.items())],
                    note='Searches all extracted modules with declared methods. Filtered matches are not selectable.')

    def list_entries_under_package(self, package_name, component_name=None):
        return [dict(name=e, reachable_methods=len(self.reached[e]), shared_resources=len(self.resources[e]))
                for e in sorted(self.entries) if self.meta[e]['package'] == package_name and
                (component_name is None or self.meta[e]['component'] == component_name)]

    def get_entry_summary(self, entry_name):
        if entry_name not in self.entries:
            raise ValueError('Unknown entry')
        partners = []
        for other in sorted(self.entries - {entry_name}):
            shared = self.resources[entry_name] & self.resources[other]
            shared &= self.writes[entry_name] | self.writes[other]
            if shared:
                partners.append(dict(entry=other, resource_count=len(shared)))
        return dict(entry=entry_name, interacting_entries=partners,
                    kinds=sorted(k for e, k in self.facts['EntryKind'] if e == entry_name))

    def subtypes(self, cls):
        """Transitive project subclasses and implementors of cls."""
        seen, todo = set(), [cls]
        while todo:
            c = todo.pop()
            for child in self.children.get(c, set()) | self.children.get('?' + c.rsplit('.', 1)[-1], set()):
                if child not in seen:
                    seen.add(child)
                    todo.append(child)
        return seen

    def type_family(self, cls):
        """cls with its transitive supertypes and subtypes (where overrides live)."""
        seen, todo = {cls}, [cls]
        while todo:
            for parent in self.parents.get(todo.pop(), ()):
                if parent not in seen:
                    seen.add(parent)
                    todo.append(parent)
        return seen | self.subtypes(cls)

    def _window(self, method_name):
        """Lexical method window from the fact location, or None when the source is unavailable."""
        location = self.locations.get(method_name)
        if not location:
            return None
        file, line, _ = location.rsplit(':', 2)
        if file not in self._texts:
            path = (self.source / file).resolve()
            self._texts[file] = path.read_text(encoding='utf-8') if path.is_relative_to(self.source) and path.is_file() else None
        return method_window(self._texts[file], int(line)) if self._texts[file] is not None else None

    def is_declaration(self, method_name):
        """Body-less interface/abstract declaration, by lexical scan from the fact location."""
        if method_name not in self._declarations:
            window = self._window(method_name)
            self._declarations[method_name] = bool(window and window['boundary_found'] and window['text'].rstrip().endswith(';'))
        return self._declarations[method_name]

    def body_kind(self, method_name):
        """'factory', 'accessor' or 'wrapper' for one-statement bodies that only create, return or forward; else None."""
        window = self._window(method_name)
        if not window or not window['boundary_found'] or '{' not in window['text']:
            return None
        body = re.sub(r'//[^\n]*|/\*.*?\*/', '', window['text'].split('{', 1)[1].rsplit('}', 1)[0], flags=re.S)
        literal = '"' in body  # a forwarded script or query is logic, not a thin wrapper
        body = re.sub(r'"(?:\\.|[^"\\])*"', '""', body)
        statements = [s.strip() for s in body.split(';') if s.strip()]
        if not statements:
            return None
        if re.match(r'return\s+new\b', statements[0]):
            return 'factory'
        if len(statements) != 1 or '{' in statements[0]:
            return None
        statement = statements[0]
        if re.fullmatch(r'(return\s+)?(this\.)?[\w$.]+(\s*=\s*[\w$.]+)?', statement):
            return 'accessor'
        return 'wrapper' if '(' in statement and not literal else None

    def is_inert(self, method_name, max_lines=INERT_MAX_LINES):
        """Short body with no state effect: no field write, call on a field, synchronization, guard or async call.
        Such methods only compute from arguments or read; a concurrency fault rarely lives there."""
        if getattr(self, '_effects', None) is None:
            self._effects = set()
            self._effects.update(m for m, _, action, _ in self.facts.get('Access', ()) if action != 'R')
            for name in ('FieldCall', 'Sync', 'Guard', 'AsyncCall'):
                self._effects.update(row[0] for row in self.facts.get(name, ()))
        if method_name in self._effects:
            return False
        window = self._window(method_name)
        return bool(window and window['boundary_found'] and len(window['text'].splitlines()) <= max_lines)

    def methods_of(self, cls):
        if getattr(self, '_by_owner', None) is None:
            self._by_owner = defaultdict(list)
            for method, owner in sorted(self.owners.items()):
                self._by_owner[owner].append(method)
        return self._by_owner.get(cls, [])

    def overrides(self, method_name):
        """Same-name, same-arity methods in project subtypes of the method's class."""
        owner = self.owners.get(method_name, method_name.rsplit('#', 1)[0])
        key = (method_name.split('#', 1)[1].split('(')[0], arity(method_name))
        return [m for sub in sorted(self.subtypes(owner)) for m in self.methods_of(sub)
                if (m.split('#', 1)[1].split('(')[0], arity(m)) == key]

    def callees(self, method_name):
        """Project methods called directly, by candidate resolution, or by virtual call on the declared receiver type."""
        if getattr(self, '_callees', None) is None:
            self._callees = defaultdict(set)
            for caller, target in self.facts['DirectCall']:
                if target in self.methods and '#<init>' not in target:
                    self._callees[caller].add(target)
            for caller, target, *_ in self.facts.get('CandidateCall', set()):
                if target in self.methods:
                    self._callees[caller].add(target)
            for caller, receiver, name, signature in self.facts['VirtualCall']:
                count = signature.strip('()').count(',') + 1 if signature.strip('()') else 0
                self._callees[caller] |= {m for m in self.methods_of(receiver)
                                          if m.split('#', 1)[1].split('(')[0] == name and arity(m) == count}
        return sorted(self._callees.get(method_name, ()))

    def callers(self, method_name):
        """Project methods whose callees() include method_name."""
        if getattr(self, '_callers', None) is None:
            self._callers = defaultdict(set)
            for caller in self.methods:
                for callee in self.callees(caller):
                    self._callers[callee].add(caller)
        return sorted(self._callers.get(method_name, ()))

    def anchor_index(self):
        if getattr(self, '_anchor_index', None) is None:
            self._anchor_index = AnchorIndex(self)
        return self._anchor_index

    def search_source(self, text, limit=SOURCE_MATCH_LIMIT):
        """Case-insensitive substring search over extracted source files; each hit names its enclosing method."""
        needle = text.strip().lower()
        if len(needle) < 3:
            raise ValueError('search_source needs at least 3 characters (an identifier, literal or message fragment)')
        index, matches, total = self.anchor_index(), [], 0
        for file in sorted(index.by_file):
            path = (self.source / file).resolve()
            if not (path.is_relative_to(self.source) and path.is_file()):
                continue
            for number, line in enumerate(path.read_text(encoding='utf-8', errors='ignore').splitlines(), 1):
                if needle in line.lower():
                    total += 1
                    if len(matches) < limit:
                        matches.append(dict(file=file, line=number, text=line.strip()[:200], method=index.enclosing(file, number)))
        return dict(query=text, matches=matches, total_matches=total,
                    note='Lexical match; method is the enclosing declaration by start line. Refine the text when truncated.')

    def constructs(self, method_name):
        """Project classes whose constructors the method calls directly."""
        return sorted({t.split('#', 1)[0] for c, t in self.facts['DirectCall']
                       if c == method_name and '#<init>' in t and t.split('#', 1)[0] in self.owners.values()})

    def report_types(self, report):
        """Classes the report points at, with the reason: named, named by a CamelCase suffix
        (ReadLock -> RedissonReadLock), a subtype of those, or created by a factory the report calls."""
        identifiers = set(re.findall(r'[A-Za-z_$][\w$]*', report))
        compound = {i for i in identifiers if i[:1].isupper() and len(re.findall(r'[A-Z][a-z0-9]+', i)) >= 2}
        reasons = {}
        for cls in sorted(set(self.owners.values())):
            simple = cls.rsplit('.', 1)[-1].rsplit('$', 1)[-1]
            if simple in identifiers:
                reasons[cls] = 'named in report'
            else:
                suffix = next((c for c in sorted(compound) if simple.endswith(c)), None)
                if suffix:
                    reasons[cls] = f'class name ends with report identifier {suffix}'
        for cls in list(reasons):
            for sub in self.subtypes(cls):
                reasons.setdefault(sub, f'subtype of {cls}')
        for method, owner in sorted(self.owners.items()):
            if owner in reasons and method.split('#', 1)[1].split('(')[0] in identifiers:
                for cls in self.constructs(method):
                    if reasons.get(cls, 'subtype of').startswith('subtype of'):  # more specific than a subtype
                        reasons[cls] = f'created by {method}, which the report calls'
        return reasons

    def report_classes(self, report):
        """Classes named in the report (simple name match) and, separately, their project subtypes."""
        identifiers = set(re.findall(r'[A-Za-z_$][\w$]*', report))
        named = {c for c in set(self.owners.values()) if c.rsplit('.', 1)[-1] in identifiers}
        return named, set().union(*(self.subtypes(c) for c in named)) - named if named else set()

    def suspicion_ranks(self):
        """Bug-agnostic DSL order (rank.py) over the whole program: method -> 1-based rank. Cached."""
        if getattr(self, '_suspicion', None) is None:
            self._suspicion = {m: i for i, m in enumerate(ranked_members(self.facts, self.reach), 1)}
        return self._suspicion

    def get_method_summary(self, method_name):
        if method_name not in self.methods:
            raise ValueError('Unknown method')
        ranks = self.suspicion_ranks() if self.dsl_prior_weight else {}
        extra = dict(dsl_suspicion_rank=ranks.get(method_name), dsl_member_count=len(ranks)) if ranks else {}
        return dict(method=method_name, **self.meta[method_name], **extra, accesses=self.accesses[method_name],
                    thread_entries=sorted(e for e, m in self.reach if m == method_name),
                    fallback_access_evidence=sorted(r for r in self.facts['AccessEvidence'] if r[0] == method_name))

    def get_interaction_dsl(self, entry_pair):
        if len(entry_pair) != 2 or len(set(entry_pair)) != 2 or not set(entry_pair) <= self.entries:
            raise ValueError('Paper mode requires two distinct declared entries')
        small = dict(self.facts)
        small['ReentrantEntry'] = set()  # paper: distinct entries, unlike extended DSL mode
        return build_interaction_dsl(small, {(e, m) for e, m in self.reach if e in entry_pair})

    def get_code_snippet(self, method_name):
        location = self.locations[method_name]
        file, line, _ = location.rsplit(':', 2)
        path = (self.source / file).resolve()
        if not path.is_relative_to(self.source):
            raise ValueError('Source path escapes root')
        start = int(line)
        window = method_window(path.read_text(encoding='utf-8'), start)
        return dict(method=method_name, file=file, start_line=start,
                    boundary_found=window['boundary_found'], note=window['note'],
                    lines=[f'{start+i}: {line}' for i, line in enumerate(window['text'].splitlines())])
