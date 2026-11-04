# FactsExtractor (schema v2)

Extracts source-level **potential** concurrency facts for ConFL. `org.extractor.Main`
runs `SourceFacts`: one parser configuration, canonical identities, source
locations, and explicit unresolved-analysis records. Usually invoked through
`python3 -m confl extract` (or automatically by `localize`), which also applies
the source prefilter.

## Build and run

Requires JDK 17+ and Maven (tested with JDK 21).

```sh
mvn -f FactsExtractor/pom.xml test package
java -jar FactsExtractor/target/ConFL-1.0-SNAPSHOT.jar demo examples/concurrency output/facts
python3 -m tools.export_dsl --facts output/facts --output output/interaction_dsl.json
```

CLI: `<project_id> <project_root_or_source_root> <facts_dir> [dependency.jar ...]`.
All `src/main/java` directories are registered for multi-module projects. The
supplied directory itself is also a source root. Pass dependency JARs explicitly
for third-party type resolution. Other custom source layouts require a suitable
source root; build configuration is not automatically executed.

## Identity and schema

Facts are UTF-8, tab-separated, sorted, and deduplicated. `schema-version.txt`
contains `2`. Regenerate all facts together; **v1 arity IDs are incompatible**.
No automatic conversion can recover types or resources lost in v1.

- Method: `demo.Example#update(java.lang.String)` (constructors use `<init>`).
- Nested class: `demo.Outer.Inner`.
- Field: `demo.Example#count` (declaration identity, not object identity).
- Lambda/task adapter: synthetic method identity containing file/line/column.
  These IDs are stable for a fixed source snapshot, not across source edits.
- Unresolved method parameter types are prefixed with `?` and diagnosed; they
  are not silently asserted to be resolved signatures.

| Relation | Columns |
|---|---|
| Class | class |
| Extends / Implements | child, parent |
| Method | method |
| InClass | method, class |
| MethodSig | method, name, parameter signature |
| DispatchMethod | concrete non-static, non-private method |
| DirectCall | caller, target |
| VirtualCall | caller, receiver type, name, parameter signature |
| AsyncCall | submitting method, task entry, source location |
| ThreadEntry / ReentrantEntry | method |
| EntryKind | method, main/task-candidate/executor/completable-future/thread |
| Access | method, resource, R/W/RW, source location |
| Guard | method, resource, access location, monitor resource |
| Sync | method, synchronization resource, operation, source location |
| Location | method, declaration location |
| Unresolved | location, diagnostic kind, detail |

`AsyncCall` is **not** a synchronous reachability edge. Lambda bodies are analyzed
separately; method-reference task adapters use virtual dispatch when appropriate.
Runnable/Callable implementations are candidate entries, not proof of scheduling.
`ReentrantEntry` permits conservative self-pair interactions when the same task
body could be invoked concurrently; executor serialization is not modeled.

## Supported evidence and limitations

- Overloads use parameter types; nested classes retain their enclosing names.
- Field resolution distinguishes local shadowing, inherited fields and other-object
  fields. String/boxed field references are retained.
- Ordinary assignments emit W, compound updates emit RW, and reads emit R.
- Lexical `synchronized` blocks/methods produce guard evidence. `Lock` and other
  synchronization calls produce events; lock/unlock control flow is not used to
  prove protection. Unknown lock expressions have site-local identities.
- Common collection mutations produce RW accesses to a `#contents` resource;
  this is a bounded library summary, not general object-state analysis.
- Direct executor submissions, CompletableFuture runAsync/supplyAsync, inline
  Thread construction/start, lambdas, method references and named task types are
  handled. Thread-variable allocation tracking, arbitrary callback frameworks,
  asynchronous continuation APIs and task aliasing are not fully modeled.
- No pointer/escape/happens-before analysis: field resources merge object instances.
  Array element writes, arbitrary mutating library calls, reflection, generated
  code, generic bridge dispatch and advanced language constructs remain incomplete.
- Field/initializer bodies are not analyzed and are reported as unsupported.
  Inspect `Unresolved.facts`; a successful command does not imply full coverage.
- Source scans exclude test/tests/testing/target/.git path components.

The analysis is a useful candidate generator, **not a sound or complete race
analysis**. Do not interpret absent facts as evidence that concurrency is absent.

## Tests

```sh
mvn -f FactsExtractor/pom.xml test package
python3 -m unittest discover -s tests -t .
```

## Incomplete-classpath recovery

The extractor now emits `CandidateCall(caller, target, location, reason)` when
method resolution fails but a source declaration can be bounded by receiver type,
imports/package, method name and arity. Ambiguous overloads remain candidates;
these are not `DirectCall` facts. No global same-name search is used for unknown
receivers. The knowledge base includes these MAY edges and preserves them in DSL metadata.

Standard `java.util` field containers can emit content reads/writes from their
source declaration even if a generic argument is unresolved. `AccessEvidence`
(method, resource, location, reason) records these operation summaries. Local
parameters shadowing a field and project-defined lookalike container types are
not treated as that field's collection contents.

Dependencies are still recommended for precision. Recovery does not make
unresolved calls exact, model external filesystem races, or eliminate existing
class-hierarchy over-approximation.

Offline regression (after building the JAR):

```sh
python3 -m unittest tests.test_unresolved_fallback
```
