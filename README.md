# ConFL

ConFL localizes concurrency bugs in Java projects. Given a project's source code and a bug report, it ranks the
methods most likely to contain the fault and writes an explainable report.

A static extractor builds a concurrency knowledge base: thread entries, call reachability, shared-resource
accesses and locks. An LLM then searches it top-down, the way a developer narrows a report down to code:

```text
component → package → pair of interacting thread entries → fault hypotheses → top-k suspicious methods
```

At the thread-pair step, an interaction DSL lists the shared resources both threads touch. The report shows this
evidence for every ranked method, next to the model's reasoning.

- [Setup](#setup): with Docker, or installed locally
- [Localize a bug in your own project](#localize-a-bug-in-your-own-project)
- [Reproduce the paper's results](#reproduce-the-papers-results)
- [Configuration](#configuration)
- [Citation](#citation)

## Setup

Both options need an API key for the model, set in your shell. The model is chosen with `--config <model name>`
(default: `gpt-6.1-sol`):

| `--config` | Provider | Key |
|---|---|---|
| `gpt-6.1-sol` (default) | OpenAI | `export OPENAI_API_KEY=...` |
| `claude-opus-5-5` | Anthropic | `export ANTHROPIC_API_KEY=...` |
| `gpt-4o-mini` | OpenAI | `export OPENAI_API_KEY=...` |

Each name is a file in `configs/`. To add another model, see [configs/README.md](configs/README.md).

Get the code:

```bash
git clone <repository-url> confl
cd confl
```

**Option A: Docker.** You only need Docker. Build the image once, inside `confl/`:

```bash
docker build -t confl .
```

The image now holds ConFL, so you can run it from any directory. Rebuild it only after changing the code.

**Option B: local install.** You need Python 3.9+ (standard library only), JDK 17+ and Maven. Build the fact
extractor once, inside `confl/`:

```bash
mvn -f FactsExtractor/pom.xml package
```

## Localize a bug in your own project

### With Docker

1. Make a working directory, and put two things in it: the project's source code, and the bug report as a text
   file. The source must match the version the bug was reported against. The report can be the title,
   description, logs and stack traces, pasted as is.

   ```bash
   mkdir my-work
   cp -r /path/to/my-project my-work/
   cp /path/to/bug.txt my-work/
   ```

   ```text
   my-work/
   ├── my-project/
   └── bug.txt
   ```

2. Run ConFL from the working directory:

   ```bash
   cd my-work
   export OPENAI_API_KEY=...
   docker run --rm -e OPENAI_API_KEY -v "$PWD":/work confl localize --source my-project --report bug.txt
   ```

   `-v "$PWD":/work` makes the current directory visible inside the container as `/work`. The source and the
   report must therefore be inside it, and `--source` and `--report` are relative to it. On Linux, add
   `--user "$(id -u):$(id -g)"` so that you own the output files.

3. Read `my-work/output/localize/bug/report.md`.

### Locally

Run ConFL from `confl/`, and point it at your files:

```bash
cd confl
export OPENAI_API_KEY=...
python3 -m confl localize --source /path/to/my-work/my-project --report /path/to/my-work/bug.txt
```

### What it does

This one command runs the whole pipeline:

1. extracts facts from the source;
2. builds the knowledge base and DSL;
3. runs the LLM-guided search;
4. writes the report.

The output goes to `output/localize/<report name>/` unless `--output` is given. The main files are:

| File | Content |
|---|---|
| `report.md` | **the explainable report** |
| `result.json` | the ranking, with an explanation per method |
| `trace.json` | every model decision and tool call |

The report covers:

- the ranking;
- the choice at each search step and its reason;
- the competing fault hypotheses;
- for each method, the model's explanation and its DSL evidence: the thread entries that reach the method, the
  resources it shares with the other thread, and whether conflicting accesses hold a common lock;
- the source code.

To try ConFL on the bundled example, run this from `confl/`:

```bash
python3 -m confl localize --source examples/concurrency --report examples/concurrency/report.txt
```

Options:

| Option | Effect |
|---|---|
| `--output DIR` | write the output to DIR instead of `output/localize/<report name>/` |
| `--top-k N` | rank N methods instead of 5 |
| `--config NAME` | use another model, e.g. `--config claude-opus-5-5` |
| `--no-report` | skip `report.md` |

Facts are cached in `<output>/facts` and reused on later runs. If the source changes, use a new `--output`
directory.

A large project takes a few minutes to extract and a few minutes to localize. One localization costs roughly
$0.3–0.5 with the default model.

## Reproduce the paper's results

The dataset has 322 concurrency bugs from 8 projects:

- the ground truth is in `ground-truth/github/`;
- the bug reports are in `examples/reports/<project>/<case>.txt`;
- the source snapshots are distributed separately, laid out as `<data>/<project>/<case>/`, e.g. `grpc/grpc_1253/`.

### Locally

Put the snapshots in `data/` (or a link to them), then:

```bash
python3 -m confl extract --dataset --jobs 4             # facts for every case, into output/facts/ (no LLM)
python3 -m confl evaluate --check                       # list cases with missing inputs (no LLM)
python3 -m confl evaluate --projects grpc --limit 10    # localize and score (costs money)
```

### With Docker

Mount the snapshots and an output directory:

```bash
docker run --rm -v /path/to/data:/opt/confl/data:ro -v "$PWD/output":/opt/confl/output confl extract --dataset
docker run --rm -e OPENAI_API_KEY -v /path/to/data:/opt/confl/data:ro -v "$PWD/output":/opt/confl/output \
    confl evaluate --projects grpc --limit 10
```

### Options and output

Options for `evaluate`:

| Option | Effect |
|---|---|
| `--projects` and `--limit` | select cases, in ground-truth order |
| `--resume` | continue a stopped run, e.g. after an API quota error |
| `--extract` | extract missing facts on the fly |

The results go to `output/evaluation/`:

| File | Content |
|---|---|
| `summary.json` | top-1/3/5, MRR and MAP |
| `project_summary.json` | the same, per project |
| `cases/<case>/` | the full record of each case, including a `report.md` that marks ground-truth matches |

A ranked method matches the ground truth when its file, class and method name agree. Overloads are not
distinguished, and constructors match by class name. Model output is not deterministic, so repeated runs differ
by a few cases.

`python3 -m confl report DIR` rewrites the reports of an earlier run without calling the model.

## Configuration

Models and settings are configured in `configs/`; see [configs/README.md](configs/README.md).

### Other parts of the repository

- `tools/` holds diagnostics that need no LLM (`python3 -m tools.<name> --help`), e.g. `dsl_coverage` and
  `export_dsl`.
- [FactsExtractor/README.md](FactsExtractor/README.md) describes the fact schema.
- Run the tests with `python3 -m unittest discover -s tests -t .` after building the extractor.

### Limitations

- The DSL evidence is static and over-approximate. Interactions are *possible*, not proven races, and locks are
  recognized only where the accessing method holds them itself.
- Thread entries come from built-in rules for common Java concurrency APIs and frameworks. Declare others with
  `knowledge_base.extra_entries`.
- Interactions are modeled within one JVM. For distributed systems, ConFL covers concurrency inside a node, but not
  races between nodes over external state such as ZooKeeper, databases or messages.
- Rankings and explanations are model judgements.

## Citation

```bibtex
@article{shao2026confl,
  title={ConFL: Explainable Concurrent Fault Localization via Hierarchy-Guided LLM Reasoning},
  author={Shao, Shuai and Wang, Dingbang and Zeng, Yiming and Yu, Tingting},
  journal={Proceedings of the ACM on Software Engineering},
  volume={3},
  number={ISSTA},
  pages={1765--1786},
  year={2026},
  publisher={ACM, New York, NY, USA}
}
```
