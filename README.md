# Deep Research Agent

A multi-agent research system that answers a question by planning, searching, reading, and
fact-checking itself — then writes a Markdown report where **every claim carries a citation
that can be mechanically verified against the source text**.

Runs entirely on free-tier APIs. No paid keys, no GPU, no vector database server.

```bash
PYTHONPATH=src python -m deepresearch.cli "How do vector databases handle deletes?" --verbose
```

---

## What makes it different from "search and summarise"

Most LLM research demos do one search, stuff the results into a prompt, and ask for a summary.
Three things here are deliberately not that.

### 1. Citations are verifiable, not asserted

The Reader agent never writes a quote. It is shown passages labelled `P1..P5`, extracted
verbatim from the page, and must cite those labels. Python maps them to run-global `[S#]` ids
and keeps the exact source text in a registry.

So a hallucinated citation is caught **with certainty, before any model opinion is involved** —
`critic.py` regexes every `[S#]` out of the draft and checks it against the registry. Only then
does an LLM judge the harder question: does the passage actually support the sentence?

Cheap deterministic check first. Expensive judgement second.

### 2. Iterative gap-filling

After the first research round, a Gap Analyst agent audits the evidence and asks *which
sub-questions are still thin?* If any are, it issues **new** search queries (rejecting rewordings
of ones already tried) and a second round runs against only those gaps. This is what makes the
research deep rather than one-shot.

### 3. Built for hostile inputs

Free tiers rate-limit, pages 404 and paywall, and small models emit broken JSON. All three are
treated as expected conditions rather than crashes:

| Failure | Handling |
|---|---|
| Rate limit (429) | Obey the provider's **own** retry hint, then walk a 4-deep fallback chain |
| Zero-quota project | Detected as permanent and skipped instantly instead of retried |
| Malformed JSON | One re-prompt carrying the validation error → typed safe default |
| Dead or paywalled URL | Source skipped; the run continues |
| Embedding model unavailable | Transparent fallback to numpy TF-IDF ranking |
| **No evidence found** | Explicit *"insufficient evidence"* report — never a fabricated answer |

Every degradation is recorded and printed under `--verbose`, so a partial run looks partial
instead of quietly wrong.

Three of these were not designed up front — they were found by running the thing and reading
the failures. Each is documented in [docs/architecture.md](docs/architecture.md#failures-found-by-running-it):

- **The fallback chain walks models before providers.** Groq rate-limits *per model*, so
  falling back `llama-3.3-70b → gpt-oss-120b → llama-3.1-8b` multiplies available throughput
  in a way a second provider cannot.
- **Retry hints are parsed, not guessed.** Providers state their own retry window
  (`try again in 1m6.599s`). A generic 2s/4s backoff against a 60-second window is guaranteed
  to fail, which it duly did.
- **Embedding inference is serialised.** Concurrent `fastembed` calls corrupt the heap and
  kill the process outright — a `double free or corruption` crash, not a Python exception.

---

## Architecture

```
  question
     │
     ▼
┌──────────┐
│ Planner  │  decompose → 3-6 sub-questions + search queries
└────┬─────┘
     │
     ▼   ┌───────────────── round loop (max 2) ─────────────────┐
     │   │                                                      │
     │   │   per sub-question, CONCURRENTLY:                    │
     │   │   ┌──────────┐    ┌──────────┐                       │
     └───┼──▶│ Searcher │───▶│  Reader  │──▶ findings + sources  │
         │   │ dedupe + │    │ fetch,   │                       │
         │   │ rank     │    │ chunk,   │                       │
         │   └──────────┘    │ retrieve │                       │
         │                   └────┬─────┘                       │
         │                        ▼                             │
         │                 ┌─────────────┐  gaps?               │
         │                 │ Gap Analyst │──── yes ─────────────┘
         │                 └──────┬──────┘
         └────────────────────────┼── no ─────────────────────────┐
                                  ▼                               │
                          ┌───────────────┐                       │
                          │  Synthesizer  │  draft with [S#]      │
                          └───────┬───────┘                       │
                                  ▼                               │
                          ┌───────────────┐  major issues         │
                          │    Critic     │───────────────────────┘
                          │ regex + LLM   │      (max 2 revisions)
                          └───────┬───────┘
                                  ▼
                            report.md
```

Full write-up of the design decisions: [docs/architecture.md](docs/architecture.md).

### On the orchestrator: it was hand-written first

The pipeline originally ran on a hand-written supervisor loop — about 40 lines of plain Python.
For *expressing* this control flow that was the right call, and I would defend it: the flow is
two cycles and a branch, and a graph DSL does not make that clearer.

It was replaced after runs started dying at the **last node**. A rate limit at the Critic
discarded planning, two research rounds, dozens of findings, and a finished draft — roughly
twenty minutes of successful work — because the orchestrator held everything in memory.

So the migration to LangGraph buys exactly one thing: **checkpointed state**. Re-run the same
question after a crash and it resumes from the last completed node instead of starting over.

What it costs, stated plainly:

- A much heavier dependency tree (`langgraph` pulls `langchain-core`)
- Control flow now spread across router functions instead of readable top-to-bottom
- Every cycle needs a terminal branch, which is easier to get wrong than a bounded `for` loop

Worth it here only because the failure it prevents is one that actually happened, twice.

---

## Stack

| Concern | Choice | Why |
|---|---|---|
| Orchestration | LangGraph `StateGraph` + `SqliteSaver` | Checkpointing, so a killed run resumes |
| Primary LLM | Groq `llama-3.3-70b-versatile` | Free tier, fast, reliable JSON mode |
| Fallback chain | 3 Groq models → `gemini-2.0-flash` | Groq limits **per model**, so models come first |
| Search | Tavily → DuckDuckGo | DDG needs no key, so the repo works with an LLM key alone |
| Extraction | `trafilatura` | Boilerplate removal |
| Embeddings | `BAAI/bge-small-en-v1.5` via `fastembed` | ONNX, ~130MB, **no PyTorch** |
| Retrieval | numpy cosine, in-memory | Per-page corpora are small; a vector DB would be ceremony |
| Schemas | `pydantic` | Every inter-agent contract is typed and validated |

---

## Setup

```bash
git clone <your-repo-url>
cd deep-research-agent

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env               # then add at least one LLM key

export PYTHONPATH=src              # src/ layout — see below
```

The packages live under `src/`, which is not importable by default — that is the point of the
layout. `./run.sh` exports `PYTHONPATH` itself, so this is only needed when invoking Python
directly. Adding a `pyproject.toml` and `pip install -e .` would remove the need entirely.

You need **at least one** LLM key. All are free and none require a credit card:

| Key | Where | Required? |
|---|---|---|
| `GROQ_API_KEY` | [console.groq.com/keys](https://console.groq.com/keys) | Recommended primary |
| `GEMINI_API_KEY` | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) | Recommended fallback |
| `TAVILY_API_KEY` | [app.tavily.com](https://app.tavily.com) | Optional — DuckDuckGo is used if blank |

The embedding model (~130MB) downloads automatically on first run.

---

## Usage

The packages live under `src/`, so export the path once per shell:

```bash
export PYTHONPATH=src
```

```bash
# Basic
python -m deepresearch.cli "What are the tradeoffs between HNSW and IVF indexes?"

# With token accounting and run statistics
python -m deepresearch.cli "How does speculative decoding work?" --verbose

# Custom output path, no live progress
python -m deepresearch.cli "What is a Merkle tree?" -o report.md --quiet

# Ignore a saved checkpoint and start over (do this after editing prompts)
python -m deepresearch.cli "How does speculative decoding work?" --fresh
```

Reports are written to `outputs/<slugified-question>.md`.

### Resuming

Runs are checkpointed per node to `cache/checkpoints.db`, keyed by a slug of the question. If a
run is interrupted — Ctrl-C, a crash, an exhausted rate limit — **just run the same question
again**. It picks up from the last completed node rather than repeating the research:

```bash
python -m deepresearch.cli "How do vector databases handle deletes?"
# ^C during the read phase

python -m deepresearch.cli "How do vector databases handle deletes?"
#   supervisor    resuming from checkpoint (next: collect)
```

Citation ids are assigned deterministically after the read fan-out, so a resumed run produces
the same `[S#]` numbering as an uninterrupted one.

### Sample output

Committed runs live in [`outputs/`](outputs/). An excerpt from
[`how-do-vector-databases-handle-deletes.md`](outputs/how-do-vector-databases-handle-deletes.md)
— 1,450 words, 36 sources, 10 unique domains, ~10 minutes, $0:

```markdown
## Impact of Deletes on Index Structures and Query Performance
### HNSW
The HNSW deletion algorithm performs a graph-repair step that reconnects
neighboring nodes [S4]. Contrasting viewpoints exist regarding the necessity
of a full rebuild: some sources claim that deleting data in HNSW indexes
requires rebuilding the entire index from scratch [S9], while others describe
a graph-repair approach that avoids full reconstruction [S4].

### IVF and PQ
No concrete claim can be made for IVF or PQ indexes, as the supplied sources
do not describe how deletions affect these index types.

## Trade-offs Between Immediate and Lazy Deletion
Evidence on deletion-vector techniques comes from Delta Lake, a table-format
system rather than a vector index. Deletion vectors add a 5-15% cost to query
execution [S10]...
```

Three behaviours worth noticing there, because they are what the architecture is *for*:

- **It refuses rather than guesses.** "No concrete claim can be made for IVF or PQ" — the
  sources did not cover it, so the report says so instead of filling the gap from the model's
  own knowledge.
- **It attributes instead of generalising.** Delta Lake evidence is explicitly labelled as a
  table format, not a vector index. An earlier version silently generalised exactly this
  source to "all vector databases"; the Critic caught it and the fix was to show the
  Synthesizer what each source actually is.
- **It surfaces disagreement.** Two sources conflict on HNSW rebuilds, and both are cited
  rather than one being quietly picked.

The reviewer note at the end lists any claim the fact-checker still disputes, with a specific
reason:

```
> - "The predominant strategy is soft deletion: vectors are marked as deleted
>    with a tombstone entry"
>   Passage [S1] describes soft deletion as a common strategy, but does not
>   state it as the predominant strategy.
```

---

## Web app

The CLI is the reference interface, but the pipeline is also wrapped in a FastAPI service with
a Streamlit frontend:

```bash
./run.sh          # both:  API on :8000, UI on :8501
./run.sh api      # backend only — interactive docs at localhost:8000/docs
./run.sh ui       # frontend only

API_PORT=8010 ./run.sh    # if something already owns :8000
BIND=0.0.0.0 ./run.sh     # expose beyond localhost — see the warning below
```

> **Both services bind to loopback by default.** The API has no authentication and will spend
> your API quota for anyone who can reach it, so exposing it on a network has to be an explicit
> choice (`BIND=0.0.0.0`). Add auth before putting this anywhere public.

The UI streams agent activity live as the run progresses, then shows the report, its sources,
and the run statistics.

### API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/research` | Enqueue a question → `{run_id, position}` (returns immediately) |
| `GET` | `/research/{id}` | Status, and the report once finished |
| `GET` | `/research/{id}/events` | **SSE** stream of agent progress |
| `GET` | `/runs` | Past runs plus resumable checkpoints |
| `GET` | `/health` | Which providers are configured, and current limits |

```bash
curl -X POST localhost:8000/research \
  -H 'content-type: application/json' \
  -d '{"question":"How do vector databases handle deletes?"}'
# {"run_id":"a1b2c3d4e5f6","status":"queued","position":0, ...}

curl -N localhost:8000/research/a1b2c3d4e5f6/events
# data: {"type":"progress","agent":"planner","message":"4 sub-questions"}
# data: {"type":"progress","agent":"reader","message":"4 findings from https://..."}
# data: {"type":"done","approved":true,"elapsed_s":214.3}
```

### One run at a time, on purpose

Requests queue behind a single worker, and the API reports your position rather than silently
starving you.

Two reasons, and the second is the real one. First, the pipeline keeps its progress callback,
token ledger and rate-limiter state in module globals, so concurrent runs in one process would
interleave. Second — and this would apply even after that were fixed — free-tier providers
cannot sustain two concurrent research runs. Two parallel jobs would rate-limit each other into
failure. Serialising is not a workaround here; it is the correct behaviour for the constraint.

`deepresearch.graph.run_research` enforces this with a process-wide lock, so the invariant
holds even if something calls the library directly.

Making runs genuinely parallel needs a per-run context threaded through every module *and*
paid API keys. That is on the roadmap, not in this build.

Notes on the design:

- No endpoint runs the pipeline inline — a run takes minutes, so `POST /research` enqueues and
  returns `202` immediately.
- Every run ends with an explicit `done` or `error` SSE event. A stream that merely stops is
  indistinguishable from a hung backend.
- Idle streams emit a keep-alive comment every 15s so proxies do not drop them during a slow
  node.
- Streamlit talks to the API over HTTP rather than importing `deepresearch`. The service
  boundary is the point; it also keeps the UI responsive while a run blocks elsewhere.

---

## Configuration

Every tunable is in [`deepresearch/config.py`](deepresearch/config.py):

```python
MAX_RESEARCH_ROUNDS     = 2    # gap-filling rounds
MAX_CRITIC_REVISIONS    = 2    # fact-check → rewrite cycles
MAX_SUBQUESTIONS        = 6
MAX_SEARCHES_TOTAL      = 12   # hard budget, enforced inside the search tool
MAX_URLS_PER_SUBQUESTION = 3
MAX_WORKERS             = 3    # concurrency, also rate-limit protection
TOP_K_PASSAGES          = 5    # passages per page shown to the Reader
```

Caching is on by default (`cache/`). Re-running the same question is much faster and consumes
no search budget. Delete `cache/` to force fresh results.

---

## Project layout

Three layers, each a package under `src/`: the engine knows nothing about HTTP, the backend
knows nothing about Streamlit, and the frontend reaches the engine only through the API.

```
src/
├── deepresearch/           # ENGINE — no web framework anywhere in here
│   ├── cli.py              # typer + rich entry point
│   ├── config.py           # every tunable, one file
│   ├── llm.py              # fallback chain, pacing, deadlines, token ledger
│   ├── graph.py            # LangGraph nodes, routers, checkpointing
│   ├── report.py           # markdown assembly + saving to outputs/
│   ├── models.py           # pydantic contracts + SourceRegistry
│   ├── runtime.py          # shared run clock, readable inside looping agents
│   ├── agents/
│   │   ├── base.py         # parse → validate → retry once → safe default
│   │   ├── planner.py      ├── reader.py      ├── synthesizer.py
│   │   └── searcher.py     └── gap_analyst.py └── critic.py
│   └── tools/
│       ├── search.py       # Tavily/DDG, dedupe, global budget
│       ├── fetch.py        # HTTP + trafilatura + disk cache
│       └── rank.py         # chunking, ONNX embeddings, TF-IDF fallback
│
├── backend/                # FastAPI
│   ├── main.py             # app factory: lifespan, CORS, router registration
│   ├── schemas.py          # request models
│   ├── routers/            # one module per resource
│   │   ├── health.py       ├── research.py
│   │   └── checkpoints.py  └── reports.py
│   └── services/
│       └── jobs.py         # single-worker queue, run registry, event fan-out
│
└── frontend/               # Streamlit
    ├── main.py             # entry point: page setup, question form, composition
    ├── api_client.py       # every HTTP call to the backend, incl. the SSE stream
    ├── config.py           # API base URL, agent colours
    └── components/
        ├── sidebar.py      # backend health, run history, checkpoints
        ├── activity.py     # live agent feed
        └── report_view.py  # metrics + Report / Run stats / Markdown tabs
```

Because this is a `src/` layout, `src` must be on `PYTHONPATH`. `run.sh` exports it; for
direct invocation see [Usage](#usage).

---

## Limitations

Stated plainly, because a research tool that overstates itself is worse than useless:

- **Citation checking verifies support, not truth.** If a source is confidently wrong, the
  report will faithfully cite something wrong. The system checks provenance, not reality.
- **Only indexed, fetchable web pages.** No PDFs, no paywalled journals, no JavaScript-only
  sites. Those are silently skipped (and counted under `--verbose`).
- **Search quality bounds everything.** DuckDuckGo without a Tavily key gives noticeably
  weaker sources — junk domains do occasionally survive into the source list.
- **There is no relevance gate on the evidence as a whole.** The "insufficient evidence"
  refusal only fires when *nothing* was found. Ask about a fictional company's real-sounding
  conference and the searcher will find a genuinely-existing conference with a similar name,
  the reader will extract valid findings from it, and the refusal path never triggers. The
  Synthesizer is instructed to call out false premises and now does so — but that is a prompt
  instruction, not an enforced check, and it is weaker than the mechanical citation
  verification elsewhere in the system. A proper fix is a relevance-scoring step between
  `collect` and `synthesize`.
- **Most calls do not run on the strongest model.** The throttle keeps each model under ~10k
  tokens/minute, and a research run saturates the primary quickly, so the majority of calls
  land on the smaller fallbacks. The chain keeps runs *alive*, which matters more, but the
  quality ceiling on a free tier is set by the fallbacks rather than by `llama-3.3-70b`.
- **Two rounds is not exhaustive.** It is a deliberate cost bound, not a claim of completeness.
- **Small free-tier models make mistakes** the Critic will not always catch. Single-model
  critique is a weaker check than an independent panel.
- **Not a substitute for reading the sources.** The report is a map of the evidence, and every
  claim links back to where it came from — follow those links for anything that matters.
- **The dependency tree is heavier than it looks.** `langgraph` pulls `langchain-core` and a
  checkpoint stack. That is a real cost for a project whose other selling point is having few
  dependencies, and it is paid for exactly one feature: resumable runs.
- **Throughput is capped by free-tier pacing.** The throttle holds each model under ~10k
  tokens/minute. A full run needs ~50k tokens, so expect minutes, not seconds. Paid keys would
  remove the pacing entirely.

## Roadmap

- Eval harness: LLM-as-judge over a golden set, scoring citation faithfulness and coverage
- Multi-critic panel with independent verifiers instead of a single reviewer
- PDF and arXiv ingestion
- Cross-source contradiction detection as a first-class report section
- Local model support via Ollama for a fully offline pipeline
