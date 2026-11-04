"""Generic Java / Netty / Future concurrency semantics applied to raw schema-v2 facts.

The extractor records syntax (how lambdas are passed, external supertypes, annotations,
calls on field-held objects); this module derives MAY thread entries, call edges and
object-state accesses from it. Rules are library semantics, never project- or GT-specific.
Every derived row keeps provenance in DerivedEntry / AccessEvidence / CandidateCall reasons.
"""
import json
import re

# --- Functional arguments -------------------------------------------------------------
# Callbacks registered on a future/stage run in whichever thread completes it.
STAGE_CALLBACK = re.compile(r'^(then\w*|whenComplete\w*|handle\w*|exceptionally\w*|addListeners?|addCallback|'
                            r'onComplete|onSuccess|onFailure|onCancel\w*|catching\w*|transform\w*|addBoth)$')
# Executor-like submission whose receiver type could not be resolved without dependency JARs.
SUBMISSION = re.compile(r'^(execute|submit|schedule\w*|invokeAll|invokeAny|newTimeout|runAsync|supplyAsync)$')
SUBMITTER_TYPE = re.compile(r'Executor|EventLoop|EventExecutor|Scheduler|ThreadPool|Pool$|Timer|Worker|Dispatcher')
FUTURE_TYPE = re.compile(r'Future|Promise|Stage|Deferred|Callback|Listenable')
# Listener/callback registration convention: the stored callback runs on the notifying thread.
REGISTRATION = re.compile(r'^((add|register|set|put)\w*(Listener|Callback|Handler|Observer|Watcher|Hook|Subscriber)s?|subscribe\w*|on[A-Z]\w*|watch\w*)$')
THREAD_TYPE = re.compile(r'(^|\.|\?)Thread$')

# --- Framework callbacks: external supertype -> callback method names ("*" = @Override methods)
CALLBACK_TYPES = {
    'io.netty.channel.ChannelInboundHandler': {'channelRead', 'channelRead0', 'channelActive', 'channelInactive', 'channelRegistered',
        'channelUnregistered', 'channelReadComplete', 'channelWritabilityChanged', 'userEventTriggered', 'exceptionCaught',
        'handlerAdded', 'handlerRemoved', 'messageReceived', 'acceptInboundMessage'},
    'io.netty.channel.ChannelOutboundHandler': {'write', 'flush', 'close', 'connect', 'disconnect', 'bind', 'deregister', 'read',
        'handlerAdded', 'handlerRemoved', 'exceptionCaught'},
    'io.netty.handler.codec': {'decode', 'decodeLast', 'encode', 'channelRead', 'channelInactive', 'exceptionCaught', 'write',
        'userEventTriggered', 'handlerRemoved'},
    'io.netty.channel.ChannelInitializer': {'initChannel'},
    'io.netty.util.concurrent.GenericFutureListener': {'operationComplete'},
    'io.netty.util.TimerTask': {'run'},
    'io.grpc.stub.StreamObserver': {'onNext', 'onError', 'onCompleted'},
    'io.grpc.ClientCall.Listener': {'onHeaders', 'onMessage', 'onClose', 'onReady'},
    'io.grpc.ServerCall.Listener': {'onMessage', 'onHalfClose', 'onCancel', 'onComplete', 'onReady'},
    'io.grpc.ServerCallHandler': {'startCall'},
    'io.grpc.ServerInterceptor': {'interceptCall'},
    'io.grpc.ClientInterceptor': {'interceptCall'},
    'com.google.common.util.concurrent.FutureCallback': {'onSuccess', 'onFailure'},
    'org.apache.zookeeper.Watcher': {'process'},
    'java.util.TimerTask': {'run'},
    'java.lang.Thread.UncaughtExceptionHandler': {'uncaughtException'},
    'javax.servlet': {'service', 'doGet', 'doPost', 'doPut', 'doDelete', 'doFilter', 'init', 'destroy'},
    'android.os.AsyncTask': {'doInBackground', 'onPostExecute', 'onProgressUpdate', 'onCancelled'},
    'android.os.Handler': {'handleMessage'},
    'android.content.ServiceConnection': {'onServiceConnected', 'onServiceDisconnected', 'onBindingDied', 'onNullBinding'},
    'jakarta.servlet': {'service', 'doGet', 'doPost', 'doPut', 'doDelete', 'doFilter', 'init', 'destroy'},
}
# Simple-name conventions for external callback interfaces (e.g. BookKeeper AsyncCallback.*Callback).
CALLBACK_SUFFIX = re.compile(r'(Handler|HandlerAdapter|Listener|Observer|Callback|Watcher|Subscriber|Interceptor|Decoder|Encoder|Codec|ImplBase|Servlet|Verticle)$')

# --- Annotated entry points (framework-invoked, typically on request/scheduler threads)
ENTRY_ANNOTATIONS = re.compile(r'(^|\.)(GET|POST|PUT|DELETE|HEAD|OPTIONS|PATCH|Scheduled|Subscribe|RequestMapping|GetMapping|PostMapping|'
                               r'PutMapping|DeleteMapping|PostConstruct|PreDestroy|LifecycleStart|LifecycleStop|OnMessage|OnOpen|OnClose|EventListener)$')

# --- Calls on field-held objects -> object-state accesses
CONTAINERS = re.compile(r'(^|\.)(Collection|List|Set|Map|SortedSet|SortedMap|NavigableSet|NavigableMap|Queue|Deque|BlockingQueue|BlockingDeque|'
                        r'HashSet|HashMap|ArrayList|LinkedList|TreeSet|TreeMap|LinkedHashSet|LinkedHashMap|ArrayDeque|PriorityQueue|'
                        r'ConcurrentMap|ConcurrentHashMap|ConcurrentSkipListMap|ConcurrentSkipListSet|ConcurrentLinkedQueue|ConcurrentLinkedDeque|'
                        r'CopyOnWriteArrayList|CopyOnWriteArraySet|LinkedBlockingQueue|ArrayBlockingQueue|LinkedBlockingDeque|PriorityBlockingQueue|'
                        r'DelayQueue|SynchronousQueue|LinkedTransferQueue|Multimap|Multiset|Table|BiMap|Cache|LoadingCache|IntObjectMap|LongObjectMap)$')
OBSERVERS = re.compile(r'^(get\w*|is\w*|has\w*|contains\w*|size|peek\w*|iterator|stream|parallelStream|forEach|entrySet|keySet|values|'
                       r'to\w*|equals|hashCode|compareTo|length|first\w*|last\w*|ceiling\w*|floor\w*|higher\w*|lower\w*|head\w*|tail\w*|'
                       r'sub\w*|descending\w*|indexOf|lastIndexOf|element|cause|join|await\w*|sync\w*|name|id|state|status|count|'
                       r'asMap|copyOf|clone|describe\w*|find\w*|lookup\w*|read\w*|list\w*|keys|query\w*|check\w*|matches|apply|accept|test|'
                       r'then\w*|whenComplete\w*|handle\w*|exceptionally\w*|addListener\w*|addCallback|onComplete|channel|pipeline|alloc|'
                       r'attr|hasAttr|executor|eventLoop|remoteAddress|localAddress|config|metadata)$')


def simple(name):
    name = name.lstrip('?')
    return name.rsplit('.', 1)[-1]


def is_async(context, callee, receiver, constructor_type):
    if context == 'constructor-argument':
        return bool(THREAD_TYPE.search(constructor_type))
    if context != 'argument':
        return False
    receiver_known = receiver not in ('-', '?') and not receiver.startswith('?')
    if STAGE_CALLBACK.match(callee):
        return not receiver_known or bool(FUTURE_TYPE.search(simple(receiver))) or receiver.startswith(('java.util.concurrent.', 'io.netty.', 'com.google.common.util.concurrent.'))
    if SUBMISSION.match(callee):
        # Resolved java.util.concurrent submissions are already AsyncCall facts from the extractor.
        return bool(SUBMITTER_TYPE.search(simple(receiver))) or not receiver_known
    return bool(REGISTRATION.match(callee))


def callback_methods(external):
    """Callback method names for an external supertype, '*' meaning any @Override method."""
    name = external.lstrip('?')
    names = set()
    for prefix, methods in CALLBACK_TYPES.items():
        # Package prefixes match whole packages; type names also match adapters/subtypes in the
        # same package (SimpleChannelInboundHandler) and unqualified spellings.
        package, kind = prefix.rsplit('.', 1)
        same_package = name.rsplit('.', 1)[0] == package or external.startswith('?')
        if name.startswith(prefix) or (kind[0].isupper() and same_package and kind in simple(name)):
            names |= methods
    if not name.startswith('java.') and CALLBACK_SUFFIX.search(simple(name)):
        names.add('*')
    return names


def state_access(callee, receiver_type, project_classes):
    """(resource suffix, action, family) for a call on a field-held object, or None."""
    receiver = receiver_type.lstrip('?')
    if receiver_type in project_classes or receiver in ('-', ''):
        return None  # project bodies are analyzed through the call graph
    if re.fullmatch(r'(byte|short|int|long|float|double|boolean|char)(\[\])*', receiver) or receiver.startswith('java.lang.String'):
        return None
    action = 'R' if OBSERVERS.match(callee) else 'RW'
    if CONTAINERS.search(receiver):
        return '#contents', action, 'container'
    if re.search(r'(^|\.)Atomic\w+$', receiver):
        return '#state', action, 'atomic'
    if FUTURE_TYPE.search(simple(receiver)):
        return '#state', action, 'future'
    if receiver.startswith('io.netty.') or re.search(r'(^|\.)(Channel|ChannelHandlerContext|ChannelPipeline|ByteBuf)$', receiver):
        return '#state', action, 'netty'
    if receiver.startswith(('java.util.concurrent.locks.', 'java.util.concurrent.Semaphore', 'java.util.concurrent.CountDownLatch')):
        return None  # synchronization objects are Sync evidence, not data
    return '#state', action, 'external-object'


def add_client_api_entries(facts, edges):
    """Optional open-world entries: public project methods with no modeled in-project caller.

    A library's client threads may invoke these concurrently, so each is a reentrant entry.
    Off by default: it raises coverage for library projects at a large DSL-size cost.
    Constructors, initializers, synthetic bodies and anonymous-class members are excluded;
    absent callers can also mean an unresolved call, which this rule cannot distinguish.
    """
    out = {name: set(rows) for name, rows in facts.items()}
    called = {target for targets in edges.values() for target in targets}
    entries = {r[0] for r in facts['ThreadEntry']}
    public = {m for m, mod in facts.get('Modifier', set()) if mod == 'public'}
    abstract = {m for m, mod in facts.get('Modifier', set()) if mod == 'abstract'}
    names = {m: name for m, name, sig in facts['MethodSig']}
    owners = dict(facts['InClass'])
    for method in sorted(public - called - entries - abstract):
        name = names.get(method, '')
        if name.startswith('<') or '@' in name or '$anonymous@' in owners.get(method, ''):
            continue
        out['ThreadEntry'].add((method,)); out['ReentrantEntry'].add((method,)); out['EntryKind'].add((method, 'client-api'))
        out['DerivedEntry'].add((method, 'client-api', 'public-without-project-caller'))
    return out


def _name_matches(actual, wanted):
    """Type or annotation name match: exact, or by trailing segments when either side is not fully qualified."""
    actual = actual.lstrip('?')
    return actual == wanted or actual.endswith('.' + wanted) or wanted.endswith('.' + actual)


def entry_rule_label(rule):
    if 'method' in rule:
        return 'method:' + rule['method']
    if 'annotation' in rule:
        return 'annotation:' + rule['annotation']
    return f"supertype:{rule['supertype']}[{','.join(rule.get('methods', ['*']))}]"


def add_declared_entries(facts, rules):
    """User-declared thread entries (knowledge_base.extra_entries), for entry points the built-in rules miss,
    e.g. methods invoked by a custom RPC or messaging framework. Each rule is one of
      {"method": "pkg.Class#name"}            methods of that name (any overload); a full method ID also works
      {"supertype": "pkg.Iface", "methods": ["name", ...]}  methods of every subtype; "*" or no list = all
      {"annotation": "KafkaListener"}         methods with that annotation
    Matched methods become reentrant entries of kind 'user-declared'. DeclaredEntryRule records, per rule,
    how many methods it matched, including methods that already were entries.
    """
    out = {name: set(rows) for name, rows in facts.items()}
    methods = {r[0] for r in facts['Method']}
    names = {m: name for m, name, sig in facts['MethodSig']}
    owners = dict(facts['InClass'])
    modifiers = {}
    for m, mod in facts.get('Modifier', set()):
        modifiers.setdefault(m, set()).add(mod)
    members = {}
    for method, owner in facts['InClass']:
        members.setdefault(owner, set()).add(method)
    parents = {}
    for child, parent in facts['Extends'] | facts['Implements'] | facts.get('ExternalSuper', set()):
        parents.setdefault(child, set()).add(parent)

    def ancestors(cls, seen=None):
        seen = set() if seen is None else seen
        for parent in parents.get(cls, ()):
            if parent not in seen:
                seen.add(parent)
                ancestors(parent.lstrip('?'), seen)
        return seen

    def callable_member(m):
        name = names.get(m, '<')
        return not name.startswith('<') and '@' not in name and not modifiers.get(m, set()) & {'static', 'abstract'}

    entries = {r[0] for r in facts['ThreadEntry']}
    out.setdefault('DerivedEntry', set())
    out['DeclaredEntryRule'] = set()
    for rule in rules:
        if 'method' in rule:
            target = rule['method']
            if '(' in target:
                matched = {target} & methods
            else:
                owner, _, name = target.rpartition('#')
                matched = {m for m in methods if names.get(m) == name and _name_matches(owners.get(m, ''), owner)}
        elif 'annotation' in rule:
            matched = {m for m, a in facts.get('Annotation', set()) if m in methods and _name_matches(a, rule['annotation'])}
        else:
            wanted = set(rule.get('methods', ['*']))
            matched = set()
            for cls in members:
                if any(_name_matches(a, rule['supertype']) for a in ancestors(cls)):
                    matched |= {m for m in members[cls] if callable_member(m) and ('*' in wanted or names[m] in wanted)}
        label = entry_rule_label(rule)
        out['DeclaredEntryRule'].add((label, str(len(matched))))
        for method in sorted(matched - entries):
            out['ThreadEntry'].add((method,)); out['ReentrantEntry'].add((method,)); out['EntryKind'].add((method, 'user-declared'))
            out['DerivedEntry'].add((method, 'user-declared', label))
        entries |= matched
    return out


def validate_entry_rules(rules):
    """Check knowledge_base.extra_entries; returns the rules unchanged."""
    if not isinstance(rules, list):
        raise ValueError('knowledge_base.extra_entries must be a list of rules')
    for rule in rules:
        keys = set(rule) if isinstance(rule, dict) else set()
        kind = keys & {'method', 'supertype', 'annotation'}
        if len(kind) != 1 or keys - kind - {'methods'} or ('methods' in keys and kind != {'supertype'}):
            raise ValueError('Each extra_entries rule needs exactly one of "method", "supertype" or "annotation" '
                             '("methods" only with "supertype"): ' + json.dumps(rule))
        value = rule[next(iter(kind))]
        if not isinstance(value, str) or not value.strip():
            raise ValueError('extra_entries values must be nonempty strings: ' + json.dumps(rule))
        if 'method' in kind and '#' not in value:
            raise ValueError('extra_entries "method" must look like pkg.Class#name: ' + json.dumps(rule))
        methods = rule.get('methods', ['*'])
        if not isinstance(methods, list) or not methods or not all(isinstance(x, str) and x for x in methods):
            raise ValueError('extra_entries "methods" must be a nonempty list of method names: ' + json.dumps(rule))
    return rules


def derive(facts):
    """Return a copy of facts with model-derived entries, edges and accesses added."""
    out = {name: set(rows) for name, rows in facts.items()}
    methods = {r[0] for r in facts['Method']}
    classes = {r[0] for r in facts['Class']}
    members = {}
    for method, owner in facts['InClass']:
        members.setdefault(owner, set()).add(method)
    entries = {r[0] for r in facts['ThreadEntry']}
    async_sites = {site for _, _, site in facts['AsyncCall']}
    derived = set()

    def add_entry(method, kind, rule, caller=None, site=None):
        if method in entries:
            return
        derived.add((method, kind, rule))
        out['ThreadEntry'].add((method,)); out['ReentrantEntry'].add((method,)); out['EntryKind'].add((method, kind))
        if caller:
            out['AsyncCall'].add((caller, method, site))

    def bodies(body):
        if body in methods:
            return [body]
        return sorted(m for m in members.get(body, ()) if '#<init>(' not in m and '#<instance-init>(' not in m)

    for caller, body, context, callee, receiver, site in sorted(facts.get('FunctionalArg', ())):
        if site in async_sites or body in entries:
            continue
        targets = bodies(body)
        if is_async(context, callee, receiver, receiver if context == 'constructor-argument' else ''):
            for target in targets:
                add_entry(target, 'async-callback', f'{context}:{callee}', caller, site)
        elif body in methods and context in ('argument', 'scope', 'constructor-argument'):
            # Lambda/reference consumed by an ordinary call: conservatively executed in the caller's thread.
            out['DirectCall'].add((caller, body))
        else:
            for target in targets:
                out['CandidateCall'].add((caller, target, site, f'functional-{context}; deferred body'))

    # External supertypes: resolved names outside the project plus import-qualified guesses.
    parents = {}
    for child, parent in facts['Extends'] | facts['Implements'] | facts.get('ExternalSuper', set()):
        parents.setdefault(child, set()).add(parent)
    ancestors = {}
    def all_ancestors(cls):
        if cls not in ancestors:
            ancestors[cls] = set()
            found = set()
            for parent in parents.get(cls, ()):
                found.add(parent)
                found |= all_ancestors(parent)
            ancestors[cls] = found
        return ancestors[cls]
    overrides = {m for m, a in facts.get('Annotation', set()) if simple(a) == 'Override'}
    static = {m for m, mod in facts.get('Modifier', set()) if mod == 'static'}
    names = {m: name for m, name, sig in facts['MethodSig']}
    for cls in sorted(classes):
        wanted = set()
        for external in all_ancestors(cls):
            if external.lstrip('?') not in classes:
                wanted |= callback_methods(external)
        if not wanted:
            continue
        for method in sorted(members.get(cls, ())):
            if method in static or '<' in names.get(method, '<') or '@' in names.get(method, ''):
                continue
            if names[method] in wanted or ('*' in wanted and method in overrides):
                add_entry(method, 'framework-callback', 'external-supertype')

    for method, annotation in sorted(facts.get('Annotation', ())):
        if method in methods and ENTRY_ANNOTATIONS.search(annotation.lstrip('?')):
            add_entry(method, 'annotated', simple(annotation))

    field_types = dict(facts.get('FieldInfo', set()))
    existing = {(m, r, s) for m, r, a, s in facts['Access']}
    guards = {}
    for method, resource, site, lock in facts['Guard']:
        guards.setdefault((method, resource, site), set()).add(lock)
    for method, field, callee, receiver, site in sorted(facts.get('FieldCall', ())):
        receiver_type = receiver if receiver not in ('?', '-') else field_types.get(field, '?')
        summary = state_access(callee, receiver_type, classes)
        if summary is None:
            continue
        suffix, action, family = summary
        resource = field + suffix
        if (method, resource, site) in existing:
            continue
        out['Access'].add((method, resource, action, site))
        out['AccessEvidence'].add((method, resource, site, f'model:{family}-call {callee}'))
        for lock in guards.get((method, field + '#state', site), ()):
            out['Guard'].add((method, resource, site, lock))
    out['DerivedEntry'] = derived
    return out
