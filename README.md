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


