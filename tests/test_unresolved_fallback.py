"""Offline integration regression for incomplete-classpath extraction.
Uses the built executable JAR; no Maven/JUnit downloads needed.
"""
import json
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
from confl.dsl import load_facts, derive_reachability, build_document

with tempfile.TemporaryDirectory(prefix='confl-fallback-') as directory:
    root = Path(directory)
    source = root / 'src/main/java/demo'; source.mkdir(parents=True)
    (source / 'Example.java').write_text('''package demo;
import absent.Path;
import java.util.Set;
class Example implements Runnable {
    public void run() { Helper.upload(null); }
}
class Helper {
    static Set<Path> existing;
    static void upload(Path path) { existing.contains(path); existing.add(path); }
    static void shadow(Set<Path> existing) { existing.add(null); }
}
class Decoy { static void upload(Path path) {} }
''')
    facts_dir = root / 'facts'
    subprocess.run(['java', '-jar', str(ROOT / 'FactsExtractor/target/ConFL-1.0-SNAPSHOT.jar'),
                    'regression', str(root), str(facts_dir)], check=True)
    facts = load_facts(facts_dir)
    target = 'demo.Helper#upload(?Path)'
    assert any(a == 'demo.Example#run()' and b == target for a,b,_,_ in facts['CandidateCall'])
    assert not any('Decoy' in b for _,b,_,_ in facts['CandidateCall'])
    assert not any(b == target for _,b in facts['DirectCall'])
    assert any(m == target and resource == 'demo.Helper#existing#contents' and action == 'RW'
               for m,resource,action,_ in facts['Access'])
    assert not any('#shadow(' in m for m,_,_,_ in facts['Access'])
    reach = derive_reachability(facts)
    assert ('demo.Example#run()', target) in reach
    strict = dict(facts, CandidateCall=set())
    assert ('demo.Example#run()', target) not in derive_reachability(strict)
    document = build_document(facts, reach)
    assert any(a['method'] == target for i in document['interactions'] for c in i['contexts'] for a in c['accesses'])
    assert document['candidate_calls'] and document['access_evidence']
    assert 'ground_truth' not in json.dumps(document)
    # Wrong containers, unrelated classes, and local shadowing must not be guessed.
    (source / 'Example.java').write_text('''package demo;
import absent.Path;
class Set<T> { void add(T value) {} }
class Example implements Runnable {
    Set<Path> existing;
    public void run() { existing.add(null); unknown.upload(null); }
}
class Helper { static void upload(Path path) {} }
''')
    subprocess.run(['java','-jar',str(ROOT / 'FactsExtractor/target/ConFL-1.0-SNAPSHOT.jar'),
                    'negative',str(root),str(facts_dir)],check=True)
    negative = load_facts(facts_dir)
    assert not any(r.endswith('#contents') for _,r,_,_ in negative['Access'])
    assert not any('Helper#upload' in b for _,b,_,_ in negative['CandidateCall'])
print('Incomplete-classpath positive/negative regressions: PASS')
