# conFL: LLM-Guided Hierarchical Concurrent Fault Localization

conFL is a research tool that automatically localizes concurrency bugs in Java projects. Given a bug report, it uses a three-stage pipeline — static fact extraction, concurrent knowledge base construction, and LLM-guided hierarchical retrieval — to rank the suspicious methods most likely responsible for the concurrency fault.

---

## Pipeline Overview

```
Java Source Code
      │
      ▼
┌─────────────────────────────────────────────────────┐
│  Stage 1 — FactsExtractor (Java)                    │
│  Parses source code → emits Datalog .facts files    │
│  (class hierarchy, call graph, field accesses,      │
│   thread entry points)                              │
└─────────────────────────────────────────────────────┘
      │  .facts files
      ▼
┌─────────────────────────────────────────────────────┐
│  Stage 2 — CKBuilder                                │
│  2a. analysis.dl (Soufflé)                          │
│      .facts → ReachInThread.csv                     │
│  2b. dsl.py                                         │
│      YAML + ground-truth → interaction_dsl.json     │
└─────────────────────────────────────────────────────┘
      │  interaction_dsl.json  +  CKB artefacts
      ▼
┌─────────────────────────────────────────────────────┐
│  Stage 3 — src (LLM-Guided Fault Localization)      │
│  Bug report → hierarchical LLM retrieval:           │
│   1. Component-Level                                │
│   2. Package-Level                                  │
│   3. Thread Entry-Level                             │
│   4. Interaction DSL Reasoning                      │
│  → ranked suspicious methods                        │
└─────────────────────────────────────────────────────┘
      │
      ▼
results.json — ranked list of suspicious methods
```

---

## Repository Structure

```
conFL-main/
├── FactsExtractor/          # Stage 1: static analysis, emits .facts files
│   ├── pom.xml
│   └── src/main/java/org/extractor/
│       ├── Main.java
│       ├── ClassMethodFactExtractor.java
│       ├── CallFactExtractor.java
│       ├── AccessFactExtractor.java
│       ├── ThreadEntryFacts.java
│       └── thread/patterns/
├── CKBuilder/               # Stage 2: thread reachability + interaction DSL
│   ├── analysis.dl          # Soufflé Datalog program
│   └── dsl.py               # DSL builder
├── src/                     # Stage 3: LLM-guided hierarchical retrieval
│   ├── run.py               # Pipeline entry point
│   ├── ckb.py               # Concurrent Knowledge Base loader
│   ├── agent.py             # Generic LLM tool-use loop
│   ├── component_level.py   # Stage 3a: component selection
│   ├── package_level.py     # Stage 3b: package selection
│   ├── thread_entry_level.py# Stage 3c: thread entry pair selection
│   ├── interaction_level.py # Stage 3d: DSL reasoning & ranking
│   └── prompt.py            # Prompt templates
├── dataset/                 # Bug reports (GitHub issues and post-based)
│   ├── GitHub/              # Bug reports from GitHub issues
│   └── Post/                # Bug reports from developer posts
├── ground-truth/            # Ground-truth buggy methods and PR mappings
│   ├── github/              # Ground truth keyed by GitHub issue
│   ├── post/                # Ground truth keyed by post
│   └── dataset/             # PR-to-issue mappings per project
└── results/                 # Baseline tool results
    ├── BRTracer/
    └── FlexFL/
```

---

## Prerequisites

- Java 16+
- Maven
- [Soufflé](https://souffle-lang.github.io/install) on `PATH`
- Python 3.8+

---

## Full Workflow

### Step 1 — Extract Datalog Facts

Build the extractor:
```bash
cd FactsExtractor
mvn package
```

Run it on a Java project:
```bash
java -jar target/facts-extractor.jar <project_id> <src_root> <facts_dir>
```

| Argument | Description |
|---|---|
| `project_id` | Identifier string, e.g. `druid_582` |
| `src_root` | Root directory of the Java project to analyse |
| `facts_dir` | Output directory for `.facts` files |

The extractor walks all non-test Java source files and emits:

| File | Content |
|---|---|
| `Class.facts` | All class names |
| `Extends.facts` | Class → superclass edges |
| `Implements.facts` | Class → interface edges |
| `Method.facts` | All method IDs |
| `InClass.facts` | Method → class mapping |
| `MethodSig.facts` | Method ID, name, and arity |
| `DirectCall.facts` | Statically resolved call edges |
| `VirtualCall.facts` | Polymorphic / unresolved call edges |
| `Access.facts` | Field reads (`R`), writes (`W`), and sync operations |
| `ThreadEntry.facts` | Thread entry point methods |

---

### Step 2a — Compute Thread Reachability (Soufflé)

```bash
cd CKBuilder
souffle analysis.dl -F <facts_dir>/ -D <output_dir>/
```

Output: `<output_dir>/ReachInThread.csv` — maps each thread entry to every reachable method.

---

### Step 2b — Build the Interaction DSL

```bash
python CKBuilder/dsl.py \
  --yaml  <path/to/druid_582.yaml> \
  --gt    <path/to/druid_582_groundtruth.txt> \
  --output interaction_dsl.json
```

| Argument | Required | Description |
|---|---|---|
| `--yaml` | Yes | YAML file produced by the analysis pipeline |
| `--gt` | Yes | Ground-truth method list (one signature per line) |
| `--output` | No | Output path (default: `interaction_dsl.json`) |

The script produces a JSON array where each element describes one shared-resource interaction across thread entries.

---

### Step 3 — Run LLM-Guided Fault Localization

```bash
cd src
python run.py \
  --bug-report druid_582_bug.txt \
  --project    druid \
  --issue-id   582 \
  --ckb-dir    /path/to/conFL \
  --dsl-dir    /path/to/datalog/dsl \
  --output     results.json
```

#### Expected CKB directory layout

```
conFL/
├── componentPackages/{project}/{case}.json
├── threadEntryPoint/{project}/{case}.json
├── classFields/{project}/{case}.json
├── methodSnippet/{project}/{case}.json
├── methodSummary/{project}/{case}.json
├── lockUsage/{project}/{case}.json
├── concurrentAPI/{project}/{case}.json
└── hierarchy/{project}/{case}.json
```

#### Output format

```json
{
  "project": "druid",
  "issue_id": "582",
  "component": "indexing-service",
  "package": "io.druid.indexing.overlord",
  "entry_pair": ["RemoteTaskRunner.runPendingTasks", "TaskMaster.start"],
  "ranked_results": [
    {
      "rank": 1,
      "method": "RemoteTaskRunner.runPendingTasks",
      "shared_resource": "ConcurrentHashMap pendingTasks",
      "explanation": "Two threads concurrently access pendingTasks without synchronization..."
    }
  ],
  "localization_text": "..."
}
```

---

## LLM Tool Functions

The LLM agent navigates the CKB through eight tool functions:

| Function | Description |
|---|---|
| `list_components` | List all components with brief descriptions |
| `list_packages_under_component(component_name)` | List all packages in the component |
| `list_entries_under_package(package_name)` | List thread entry points in the package |
| `list_method_under_class(class_name)` | List all methods in the class |
| `get_entry_summary(entry_name)` | Concurrency summary of the thread entry |
| `get_method_summary(method_name)` | Concurrency-related info of the method |
| `get_interaction_dsl(entry1, entry2)` | Shared-resource interaction DSL for the thread-entry pair |
| `get_code_snippet(method_name)` | Source code snippet of the method |
| `exit` | Terminate the retrieval process |

---

## Dataset

Bug reports are in `dataset/` organized by source:

- **GitHub/** — reports extracted from GitHub issues (projects: druid, grpc, presto, pulsar, redisson, rocketmq, trino, vertx)
- **Post/** — reports extracted from developer posts (projects: camel, cassandra, druid, grpc, hadoop, hbase, pulsar, redisson)

Ground-truth buggy methods and PR mappings are in `ground-truth/`.

Baseline results from BRTracer and FlexFL are in `results/`.

---

## Complete Example (druid #582)

```bash
# Step 1: extract facts
java -jar FactsExtractor/target/facts-extractor.jar \
  druid_582 \
  /path/to/druid_582/src \
  facts/druid_582/

# Step 2a: compute thread reachability
souffle CKBuilder/analysis.dl \
  -F facts/druid_582/ \
  -D output/druid_582/

# Step 2b: build interaction DSL
python CKBuilder/dsl.py \
  --yaml  ~/Desktop/condata/datalog/dsl/druid_582.yaml \
  --gt    ground-truth/github/druid.json \
  --output dsl/druid_582_dsl.json

# Step 3: run fault localization
export ANTHROPIC_API_KEY=sk-ant-...
cd src
python run.py \
  --bug-report ../dataset/GitHub/druid/druid_582.txt \
  --project    druid \
  --issue-id   582 \
  --output     druid_582_results.json
```
