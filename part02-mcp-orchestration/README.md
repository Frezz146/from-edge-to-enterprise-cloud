# Part 02: MCP Orchestration

Connects the local model from Part 1 to external tools through the Model
Context Protocol (MCP), then answers the actual question this part is
about: **when a tool's JSON schema gets complex, how much more often does a
compact local SLM get the call wrong compared to a cloud frontier model -
and does hardening the schema actually fix it?**

Every example in this part is self-contained per language: the Python
track (server + clients) never needs C# or .NET installed, and the C#
track never needs Python. Each language has its own MCP server exposing
the same conceptual tools with its own idiomatic schema (snake_case field
names in Python via Pydantic, camelCase in C# via System.Text.Json's
defaults) - not a byte-identical schema shared across a language boundary.

Both languages are built directly on the raw MCP and chat-completion SDKs -
no higher-level agent framework in between. That's a deliberate choice, not
an oversight: this part's whole point is measuring tool-call reliability
and error rates, which means inspecting each attempt's raw arguments and
outcome. A framework that auto-executes tool calls for you and only hands
back the final text would hide exactly the detail this part exists to
look at.

This part ships three things per language:

1. **A standalone MCP server** (`mcp_server.py` / `csharp/mcp-server`) -
   four tools: `get_weather`, `calculate` (the quickstart pair) and
   `book_appointment_loose` / `book_appointment_strict` (the schema study
   pair).
2. **A quickstart client** (`mcp_client.py` / `csharp/mcp-agent`) - connect,
   discover tools, run one tool-calling turn.
3. **The schema hardening study** (`schema_hardening_eval.py` /
   `csharp/schema-hardening-eval`) - the actual measurement: an error
   taxonomy instead of a pass/fail bit, run across both schema variants and
   (in Python) both a local and a cloud model. Python and C# use the
   *same* taxonomy - see below.

If you only read one section before writing the blog post, read
"The schema hardening study" below - that's where the numbers live.

## Why MCP here

MCP separates "what a tool looks like" from "which model is calling it". A
server declares tools with a JSON schema; any MCP-speaking client discovers
that schema at runtime and hands it, unchanged, to whichever chat backend is
calling - a 0.5B local model or a frontier model in the cloud.

## Quickstart

### Python: `mcp_client.py`

```bash
cd part02-mcp-orchestration/python
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt

python mcp_client.py                 # local model only
python mcp_client.py --compare-cloud # + Azure OpenAI, if AZURE_OPENAI_* env vars are set
```

`mcp_client.py` spawns `mcp_server.py` over stdio automatically - no need to
run the server separately. At the end it prints a reliability summary: how
many of the prompts that required a tool call actually produced one, per
backend. This is intentionally the simple version - see the study below for
the version that also checks whether the call was actually *correct*.

### C#: `mcp-agent`

```bash
cd part02-mcp-orchestration/csharp/mcp-agent
dotnet run
```

Connects to the standalone C# server in `../mcp-server` (built automatically
on first run via `dotnet run --project`), then runs one tool-calling turn
against the local model using a hand-declared tool schema mirroring
`GetWeather`.

## The schema hardening study

Both MCP servers expose the same conceptual task - "book a meeting" -
through two tools:

| | loose | strict |
| --- | --- | --- |
| Attendee | two flat strings (`attendee_name` / `attendee_email` in Python, `attendeeName` / `attendeeEmail` in C#) | nested object with `name` / `email` |
| Topics | one free-text string | array of strings |
| Priority | untyped string, described as "how urgent" | enum: `low` \| `medium` \| `high` |
| Duration | untyped string, described as "how long" | integer |
| Server-side validation | none - any string is accepted | rejected before the handler ever runs if the shape is wrong (Pydantic in Python, System.Text.Json model binding in C#) |

Same information, same task - only the schema's rigor differs. Three
prompts of increasing phrasing difficulty (explicit values -> multiple
topics -> informal wording like "low-key" and "nothing urgent") are run
against each (model, schema) combination, and every tool call is classified
into exactly one outcome - the same taxonomy in both languages, since both
inspect the raw chat completion response directly rather than going through
a framework that would obscure some of these:

| Category | What it means |
| --- | --- |
| `NO_TOOL_CALL` | The model answered instead of calling the tool |
| `WRONG_TOOL` | It called a different tool than the one offered |
| `MALFORMED_JSON` | The tool-call arguments weren't valid JSON |
| `SERVER_REJECTED` | The MCP server's own schema validation rejected the call - only possible against the strict tool, since the loose one has no types to violate |
| `WRONG_NAME` / `WRONG_EMAIL` / `WRONG_PRIORITY` / `WRONG_DURATION` / `MISSING_TOPIC` | The call succeeded, but a value doesn't match what the prompt asked for - what the loose schema lets slip through silently |
| `OK` | Every field matches |

### Run it

```bash
cd part02-mcp-orchestration/python
python schema_hardening_eval.py                 # local model only
python schema_hardening_eval.py --compare-cloud  # + Azure OpenAI
```

```bash
cd part02-mcp-orchestration/csharp/schema-hardening-eval
dotnet run   # local model only - see the Python script for --compare-cloud
```

Both print a per-case classification and a summary table, e.g.:

```
Backend                | Schema | Clean calls | Errors
------------------------------------------------------------------------
local (qwen2.5-0.5b)    | loose  | 1/3         | WRONG_PRIORITY x1, WRONG_DURATION x1
local (qwen2.5-0.5b)    | strict | 3/3         | -
cloud (Azure OpenAI)    | loose  | 2/3         | WRONG_DURATION x1
cloud (Azure OpenAI)    | strict | 3/3         | -
```

(Illustrative shape - run it yourself for real numbers; they depend on your
hardware, your Azure deployment, and the exact model build.) The Python
script additionally saves `schema-hardening.png`, a bar chart of clean-call
rate by (backend, schema variant) - not tracked in git since, like
`latency-benchmark.png` in Part 1, the numbers are yours to regenerate.

### Why this design, not a single "hard schema" test

A single complex tool with no point of comparison only shows that a small
model *can* fail - it doesn't show that the schema is *why*. Running the
identical task through a loose and a strict version of the same tool
isolates the one variable this part's core question is actually about:
holding the task constant, does tightening the schema change the error
rate? The `SERVER_REJECTED` category exists because server-side validation
on the strict tool catches type/enum violations *before* they'd otherwise
succeed silently - that's the concrete mechanism behind "harden your
schemas," not just an assertion.

## What to look for (for the blog)

- **The loose/strict clean-rate gap for the local model** is the headline
  number - it's the direct answer to "how much does hardening help a small
  model."
- **Which error categories dominate the loose column** tells the more
  specific story: if it's mostly `WRONG_PRIORITY`, the lesson is "use
  enums"; if it's mostly `WRONG_DURATION`, it's "use typed numbers, not
  free-text units."
- **Whether `SERVER_REJECTED` ever fires** is worth calling out explicitly -
  it means the hardened schema turned a silent wrong answer into a loud,
  catchable protocol error the caller can retry or escalate on, which a
  loose schema can never do no matter how good the model is.
- **The gap between the local and cloud columns, per schema variant** shows
  whether hardening closes the small-vs-frontier-model gap or just lowers
  both error rates in parallel - run `--compare-cloud` to get that number.

## A note on "flat vs. nested" as a hardening strategy

It's tempting to conclude that small models do better with *simpler*
(flatter) schemas. That's not quite what this study shows: the strict tool
here is *more* structured than the loose one - a nested object, an array, an
enum - and it's the more reliable one, because the added structure is also
what lets the server catch and reject a malformed call. The lesson isn't
"avoid structure," it's "specify precisely" - enums instead of free text,
typed fields instead of stringly-typed ones, required fields instead of
optional guesses. A flat schema that's still vague (untyped strings, no
enum, no required list) gets none of that benefit just for being flat.

## Files in this part

```text
part02-mcp-orchestration/
├── python/
│   ├── requirements.txt          foundry-local-sdk, mcp; optional openai, matplotlib
│   ├── mcp_server.py              standalone MCP server: get_weather, calculate, book_appointment_loose/strict
│   ├── mcp_client.py              quickstart client - raw MCP SDK, hand-rolled tool-calling loop
│   └── schema_hardening_eval.py   the schema hardening study - full error taxonomy
└── csharp/
    ├── mcp-server/                 standalone MCP server, C# (ModelContextProtocol server hosting)
    │   ├── mcp-server.csproj
    │   └── Program.cs
    ├── mcp-agent/                  quickstart client, C#
    │   ├── mcp-agent.csproj
    │   └── Program.cs
    └── schema-hardening-eval/      the schema hardening study, C#
        ├── schema-hardening-eval.csproj
        └── Program.cs
```

Each `mcp_server.py` / `csharp/mcp-server` is spawned automatically by every
client in its own language over stdio - you never run a server by hand,
and neither language's client ever spawns the other language's server (see
"self-contained per language" above). The two non-server projects per
language exist because they answer different questions: `mcp_client.py` /
`mcp-agent` is the five-minute "does this work" quickstart, with a coarse
reliability check; `schema_hardening_eval.py` / `schema-hardening-eval` is
the actual measurement this part's blog post is about, with the full error
taxonomy - keeping them separate means you can read the quickstart in
isolation without wading through the classification logic.
`schema-hardening.png` is a run-time output of the Python study,
git-ignored for the same reason `latency-benchmark.png` is in Part 1: the
numbers are yours to regenerate, not something to commit.
