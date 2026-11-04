# Model configurations

Each file configures one model and is selected by its name:

```bash
python3 -m confl localize --config claude-opus-5-5 --source my-project --report bug.txt
```

Without `--config`, `gpt-6.1-sol` is used.

| Name | Provider | API key variable | Note |
|---|---|---|---|
| `gpt-6.1-sol` | OpenAI | `OPENAI_API_KEY` | default; used for the reported results |
| `claude-opus-5-5` | Anthropic | `ANTHROPIC_API_KEY` | |
| `gpt-4o-mini` | OpenAI | `OPENAI_API_KEY` | cheap, but noticeably weaker |

## Adding a model

1. Copy the configuration of a model from the same provider. Name the copy after the new model:

   ```bash
   cp configs/gpt-6.1-sol.json configs/my-model.json
   ```

2. Edit the `llm` section of the copy:

   | Key | Meaning |
   |---|---|
   | `provider` | `openai` or `anthropic`; this selects the API format |
   | `model` | the model ID sent to the API |
   | `api_key_env` | the name of the environment variable that holds the key; never the key itself |
   | `base_url` | the API endpoint |
   | `max_output_tokens` | the output budget per request |
   | `timeout_seconds` | the request timeout |
   | `effort` | optional, Anthropic only: the effort level, e.g. `medium` |
   | `pricing` | optional: USD per million tokens (`input`, `cached_input`, `cache_write`, `output`), used only for cost estimates |

   `base_url` can point to another HTTPS service that implements the same API: the OpenAI Responses API for
   `openai`, or the Anthropic Messages API for `anthropic`. The model must support tool calling.

3. Run it with `--config my-model`.

### Docker

The image contains only the configurations that existed when it was built. To use a new one, either rebuild the
image, or mount the file into the image's configuration directory:

```bash
docker run --rm -e OPENAI_API_KEY -v "$PWD":/work -v "$PWD/my-model.json":/opt/confl/configs/my-model.json:ro \
    confl localize --config my-model --source my-project --report bug.txt
```

## Declaring thread entries

ConFL finds thread entries with built-in rules:

- threads, executors and futures;
- registered listeners and callbacks;
- callbacks of Netty, gRPC, ZooKeeper and servlets;
- common annotations such as `@Scheduled`, `@GET` and `@RequestMapping`;
- implementations of external interfaces named `*Handler`, `*Listener`, `*Callback` and so on.

A framework these rules do not know, such as a custom RPC layer, a message consumer or a consensus state machine,
calls its handlers through code the extractor does not see. Those handlers then reach nothing, and the DSL shows no
interactions for them. Declare them in `knowledge_base.extra_entries`:

```json
"extra_entries": [
  {"supertype": "org.apache.thrift.TProcessor"},
  {"supertype": "com.example.rpc.RequestHandler", "methods": ["handle", "onTimeout"]},
  {"annotation": "KafkaListener"},
  {"method": "com.example.raft.StateMachine#apply"}
]
```

| Rule | Matches |
|---|---|
| `supertype` | instance methods of every class that extends or implements the type, inside or outside the project. Use `methods` to restrict the method names; without it, all methods match. |
| `annotation` | methods with that annotation |
| `method` | methods with that class and name, every overload; a full method ID such as `pkg.Class#name(java.lang.String)` selects one |

- Type and annotation names may be simple (`TProcessor`) or qualified.
- Static methods, constructors and abstract declarations are never entries.
- Declared entries are treated like built-in ones and may run concurrently with themselves.
- `localize` and `evaluate` print how many methods each rule matched. The report lists the rules, and marks
  user-declared entries. A rule that matches nothing usually has a misspelled name.
- Facts do not need to be extracted again after you change the rules.

Each declared entry is in-process: ConFL still finds interactions only through shared memory within one JVM. Races
between processes, over external state such as ZooKeeper nodes, database rows or messages, are not modeled.

## Other sections

The other sections are the same in every file. Usually you leave them unchanged:

| Section | Keys |
|---|---|
| `knowledge_base` | `exclude_components` (components the search ignores), `extra_entries` (see above), `min_thread_entries`, `dsl_prior_weight` (weight of the static DSL ranking in retrieval; 0 disables), `client_api_entries` |
| `extraction` | `exclude_modules` (module paths to skip, e.g. connectors of a very large project), `java_options` (e.g. `["-Xmx8g"]`), `jobs`, `prefilter`, `extractor_jar` |
| `localization` | `top_k` |
| `dataset` | where `evaluate` finds the ground truth, sources, reports and facts, and where it writes results |

Relative paths resolve against the configuration file, so keep your configuration files in `configs/`.

`--config` also accepts a file path, e.g. `--config /path/to/my-model.json`. In that case, use absolute paths
inside the file.
