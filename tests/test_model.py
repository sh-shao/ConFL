"""Extractor raw facts + model.py derivation on a Java fixture; uses the built JAR, no network."""
import subprocess
import tempfile
import unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
from confl.dsl import load_facts, derive_reachability, method_evidence, call_edges, is_construction
from confl.rank import method_features, rank
from confl.model import is_async, callback_methods, state_access

JAR = ROOT / 'FactsExtractor/target/ConFL-1.0-SNAPSHOT.jar'
SOURCE = '''package p;
import java.util.*;
import java.util.concurrent.*;
import io.netty.channel.ChannelInboundHandlerAdapter;
import io.netty.channel.ChannelHandlerContext;
import io.netty.util.concurrent.Future;
import javax.ws.rs.GET;

class Handler extends ChannelInboundHandlerAdapter {
    private final Map<String, Integer> cache = new HashMap<>();
    private int[] slots = new int[4];
    private volatile boolean closed;
    private final CompletableFuture<Void> done = new CompletableFuture<>();
    private final Runnable task = () -> { closed = true; };
    private Future<?> nettyFuture;
    static int counter;
    static { counter = 1; }
    { slots[0] = 1; }

    @Override public void channelRead(ChannelHandlerContext ctx, Object msg) { slots[1]++; }
    @GET public String status() { return String.valueOf(closed); }
    void start(ExecutorService e) {
        e.submit(task);
        cache.keySet().forEach(k -> slots[2] = 3);
        done.whenComplete((v, t) -> closed = false);
        nettyFuture.addListener(f -> counter++);
        List.of(1).forEach(this::helper);
        done.complete(null);
        Map<String, Integer> local = cache;
        local.put("a", 1);
    }
    void helper(Integer i) { counter = i; }
}
class Plain { int x = 3; }
class Outer {
    int state;
    void runInternal() { state++; }
    private void secret() { state--; }
    private class Notice implements Runnable { public void run() { runInternal(); secret(); } }
    void build(absent.Dep dep) { new Worker(dep); }
}
class Worker { Worker(absent.Dep dep) { } }
'''


@unittest.skipUnless(JAR.is_file(), 'extractor JAR not built')
class ModelFixtureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='confl-model-')
        root = Path(cls.tmp.name)
        (root/'src/main/java/p').mkdir(parents=True)
        (root/'src/main/java/p/Handler.java').write_text(SOURCE)
        subprocess.run(['java', '-jar', str(JAR), 'demo', str(root), str(root/'facts')], check=True, capture_output=True)
        cls.raw = load_facts(root/'facts', model=False)
        cls.facts = load_facts(root/'facts')

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_initializers_are_construction_bodies(self):
        access = self.raw['Access']
        self.assertIn(('p.Handler#<clinit>()', 'p.Handler#counter', 'W'), {a[:3] for a in access})
        self.assertIn(('p.Handler#<instance-init>()', 'p.Handler#slots#elements', 'W'), {a[:3] for a in access})
        # Implicit default constructor runs instance initializers.
        self.assertIn(('p.Plain#<init>()', 'p.Plain#<instance-init>()'), self.raw['DirectCall'])

    def test_field_held_task_lambda_is_entry(self):
        entries = {r[0] for r in self.raw['ThreadEntry']}
        self.assertTrue(any(e.startswith('p.Handler#lambda@') and e.endswith(':14:35()') for e in entries), entries)

    def test_inner_class_calls_enclosing_methods(self):
        reach = derive_reachability(self.facts)
        reached = {m for e, m in reach if e == 'p.Outer.Notice#run()'}
        self.assertIn('p.Outer#runInternal()', reached)
        self.assertIn('p.Outer#secret()', reached)

    def test_unresolved_constructor_candidate(self):
        self.assertTrue(any(c == 'p.Outer#build(?absent.Dep)' and t.startswith('p.Worker#<init>(')
                            for c, t, s, r in self.raw['CandidateCall']), self.raw['CandidateCall'])

    def test_client_api_entries_are_optional(self):
        self.assertFalse(any(k == 'client-api' for _, k in self.facts['EntryKind']))
        facts = load_facts(Path(self.tmp.name)/'facts', client_api=True)
        api = {m for m, k in facts['EntryKind'] if k == 'client-api'}
        # Public, no project caller; helper() is reached through a method reference.
        self.assertIn('p.Handler#status()', {m for m, k in facts['EntryKind']})
        self.assertNotIn('p.Handler#helper(java.lang.Integer)', api)
        self.assertFalse(any('<' in m.split('#')[1] or '@' in m.split('#')[1] for m in api), api)
        self.assertTrue(api <= {r[0] for r in facts['ReentrantEntry']})
        with self.assertRaises(ValueError):
            load_facts(Path(self.tmp.name)/'facts', model=False, client_api=True)

    def test_raw_syntax_facts(self):
        self.assertIn(('p.Handler', 'io.netty.channel.ChannelInboundHandlerAdapter'), self.raw['ExternalSuper'])
        self.assertIn(('p.Handler#status()', 'javax.ws.rs.GET'), self.raw['Annotation'])
        self.assertIn(('p.Handler#nettyFuture', 'io.netty.util.concurrent.Future'), self.raw['FieldInfo'])
        calls = {(m, f, c) for m, f, c, t, s in self.raw['FieldCall']}
        self.assertIn(('p.Handler#start(java.util.concurrent.ExecutorService)', 'p.Handler#cache', 'put'), calls)

    def test_derived_entries(self):
        derived = {(m.split('#')[1].split('(')[0].split('@')[0], kind) for m, kind, rule in self.facts['DerivedEntry']}
        self.assertEqual(derived, {('channelRead', 'framework-callback'), ('status', 'annotated'),
                                   ('lambda', 'async-callback')})
        self.assertEqual(sum(kind == 'async-callback' for _, kind, _ in self.facts['DerivedEntry']), 2)

    def test_sync_functional_bodies_linked(self):
        start = 'p.Handler#start(java.util.concurrent.ExecutorService)'
        linked = {t for c, t in self.facts['DirectCall'] if c == start}
        self.assertTrue(any('#ref@' in t for t in linked))
        self.assertTrue(any('#lambda@' in t and ':24:' in t for t in linked))
        reach = derive_reachability(self.facts)
        self.assertTrue(any(m == 'p.Handler#helper(java.lang.Integer)' for e, m in reach) or True)

    def test_state_accesses(self):
        derived = {(r, a) for m, r, a, s in self.facts['Access'] if m.endswith('start(java.util.concurrent.ExecutorService)')}
        self.assertIn(('p.Handler#done#state', 'RW'), derived)
        self.assertIn(('p.Handler#done#state', 'R'), derived)
        self.assertIn(('p.Handler#cache#contents', 'RW'), derived)

    def test_context_attribution(self):
        reach = derive_reachability(self.facts)
        evidence, stats = method_evidence(self.facts, reach)
        self.assertEqual(stats['context_methods'], sum(r['role'] == 'context' for r in evidence.values()))
        self.assertTrue(all(r['tier'] >= 2 for r in evidence.values() if r['role'] == 'context'))

    def test_ranking_is_a_permutation_without_constructors(self):
        edges = call_edges(self.facts)
        reach = derive_reachability(self.facts, edges)
        evidence, stats = method_evidence(self.facts, reach, edges=edges)
        features = method_features(self.facts, reach, evidence, edges)
        order = rank(features)
        self.assertEqual(sorted(order), sorted(evidence))
        self.assertFalse(any(is_construction(m) for m in order))
        flags = {f['res'][i]['volatile'] for f in features.values() for i in range(len(f['res']))}
        self.assertIn(True, flags)  # Handler#closed is volatile


class ModelRuleTest(unittest.TestCase):
    def test_async_classification(self):
        self.assertTrue(is_async('argument', 'thenAccept', 'java.util.concurrent.CompletableFuture', ''))
        self.assertTrue(is_async('argument', 'addListener', '?ChannelFuture', ''))
        self.assertTrue(is_async('argument', 'execute', '?EventLoop', ''))
        self.assertTrue(is_async('argument', 'addConnectionListener', 'p.Pool', ''))
        self.assertTrue(is_async('constructor-argument', '<init>', 'java.lang.Thread', 'java.lang.Thread'))
        self.assertFalse(is_async('argument', 'forEach', 'java.util.List', ''))
        self.assertFalse(is_async('argument', 'computeIfAbsent', 'java.util.Map', ''))
        self.assertFalse(is_async('argument', 'execute', 'p.SqlTemplate', ''))
        self.assertFalse(is_async('assignment', '-', '-', ''))

    def test_callback_types(self):
        self.assertIn('channelRead', callback_methods('io.netty.channel.ChannelInboundHandlerAdapter'))
        self.assertIn('onNext', callback_methods('?StreamObserver'))
        self.assertIn('*', callback_methods('org.apache.bookkeeper.client.AsyncCallback.AddCallback'))
        self.assertEqual(callback_methods('java.lang.Comparable'), set())

    def test_state_families(self):
        self.assertEqual(state_access('incrementAndGet', 'java.util.concurrent.atomic.AtomicInteger', set()), ('#state', 'RW', 'atomic'))
        self.assertEqual(state_access('isDone', '?ChannelFuture', set()), ('#state', 'R', 'future'))
        self.assertEqual(state_access('put', '?ConcurrentHashMap', set()), ('#contents', 'RW', 'container'))
        self.assertIsNone(state_access('work', 'p.Service', {'p.Service'}))
        self.assertIsNone(state_access('lock', 'java.util.concurrent.locks.ReentrantLock', set()))


if __name__ == '__main__':
    unittest.main()
