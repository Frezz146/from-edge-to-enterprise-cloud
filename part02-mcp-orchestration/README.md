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
| Duration | integer, same as strict - a duration is inherently numeric even in an otherwise loose schema, so this one field isn't a differentiator between the two tools | integer |
| Server-side validation | only what the type system still enforces (duration must actually be a number; everything else is an unchecked string, including priority and topic content) | rejected before the handler ever runs if the shape is wrong (Pydantic in Python, System.Text.Json model binding in C#) |

Same information, same task - only the schema's rigor differs. Ten prompts
of increasing phrasing difficulty (explicit values -> multiple topics ->
informal wording like "low-key", "nothing urgent" and "standard-priority")
are run against each (model, schema) combination, and every tool call is
classified into exactly one outcome - the same taxonomy in both languages,
since both inspect the raw chat completion response directly rather than
going through a framework that would obscure some of these. Ten cases
rather than a handful gives a clean-call rate at 10% resolution instead of
33% buckets - enough to actually show a gap, not just gesture at one.

| Category | What it means |
| --- | --- |
| `NO_TOOL_CALL` | The model answered instead of calling the tool |
| `WRONG_TOOL` | It called a different tool than the one offered |
| `MALFORMED_JSON` | The tool-call arguments weren't valid JSON |
| `SERVER_REJECTED` | The MCP server's own schema validation rejected the call - happens almost exclusively against the strict tool (nested object, enum, typed fields), plus the one thing the loose tool still enforces: duration must actually be a number, not free text |
| `WRONG_NAME` / `WRONG_EMAIL` / `WRONG_PRIORITY` / `WRONG_DURATION` / `MISSING_TOPIC` | The call succeeded, but a value doesn't match what the prompt asked for - what the loose schema lets slip through silently |
| `OK` | Every field matches |

Some models don't emit an OpenAI-style tool call at all - they write it as
plain text in their own tag format instead (observed: the qwen3.5 family via
Foundry Local, e.g. `<tool_call><function=book_appointment_loose><parameter=
attendee_name>Jamie Chen</parameter>...</function></tool_call>`). Counting
that as `NO_TOOL_CALL` would conflate "the client didn't recognize this call
format" with "the model didn't try" - two very different findings. The
Python script recovers these with a small fallback parser: this tag format
carries no quoting, so a bare `high` or `30` in a `<parameter>` tag can't be
told apart from a JSON string vs. a JSON number by its text alone - the
parser is schema-aware instead, decoding each parameter as JSON only when
the target tool's own schema says that field isn't a plain string, and
keeping it as literal text otherwise. This is what makes the recovered call
comparable to a real tool call's already-typed JSON arguments, whatever the
current tool schemas' field types happen to be. It then classifies the
recovered call normally, so the summary table reflects the model's actual
argument accuracy instead of a client-side parsing gap. The C# study does
the same. If you see this path trigger, the console prints
`(client didn't parse the tool call - recovered via fallback parser: ...)`
so you can tell which rows in the summary came from it.

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
local (qwen2.5-0.5b)    | loose  | 6/10        | WRONG_DURATION x1, MISSING_TOPIC x1, WRONG_PRIORITY x1, WRONG_EMAIL x1
local (qwen2.5-0.5b)    | strict | 0/10        | SERVER_REJECTED x8, WRONG_PRIORITY x1, MISSING_TOPIC x1
cloud (Azure OpenAI)    | loose  | 9/10        | WRONG_PRIORITY x1
cloud (Azure OpenAI)    | strict | 10/10       | -
```

(Illustrative shape - run it yourself for real numbers; they depend on your
hardware, your Azure deployment, and the exact model build.) The Python
script additionally saves `schema-hardening.png`, a bar chart of clean-call
rate by (backend, schema variant) - not tracked in git since, like
`latency-benchmark.png` in Part 1, the numbers are yours to regenerate.

### Sweeping model sizes within a family

All four scripts (`mcp_client.py`, `schema_hardening_eval.py`, `mcp-agent`,
`schema-hardening-eval`) accept `--model` followed by **one or more**
aliases - the default is `qwen2.5-0.5b` everywhere. Pass several and each
script loads, tests and unloads them one after another, adding a row per
model to the same summary table instead of overwriting the previous run:

```bash
cd part02-mcp-orchestration/python
python schema_hardening_eval.py --model qwen2.5-1.5b qwen2.5-7b qwen3.5-0.8b qwen3.5-2b qwen3.5-4b
```

```bash
cd part02-mcp-orchestration/csharp/schema-hardening-eval
dotnet run -- --model qwen2.5-1.5b qwen2.5-7b qwen3.5-0.8b qwen3.5-2b qwen3.5-4b
```

Those five are a reasonable sweep to demonstrate the point of this section:
`qwen2.5-1.5b` / `qwen2.5-7b` are larger steps within the same family as
the `qwen2.5-0.5b` default, and `qwen3.5-0.8b` / `qwen3.5-2b` / `qwen3.5-4b`
add a newer generation at comparable sizes, so you can see whether moving
up in scale or moving up in generation is what actually closes the gap.
Run `foundry model list` first to see which of these (or any other alias)
are actually cached or downloadable for your hardware and Foundry Local
version - the exact catalog is served dynamically and isn't something this
repo can hardcode, and not every alias above may be available yet depending
on when you're reading this.

This is the concrete way to check whether the local model's strict-schema
`SERVER_REJECTED` result above is a `qwen2.5-0.5b`-specific ceiling or
holds for the whole family: run the identical study across several sizes
and compare each strict clean-call rate to the 0.5B row and to the cloud
row - if it climbs toward the cloud number as size (or generation)
increases, structural capability (not schema design) was the bottleneck;
if it doesn't move much even at the largest size you tested, the schema's
rigor genuinely is the harder part for this model family. There's a third
possible outcome, though, and it's the one an actual sweep turned up - see
below.

### A bigger model in the same family can regress - and why

Running the sweep above turned up a result that cuts against "bigger closes
the gap": `qwen2.5-7b`, larger than the `qwen2.5-1.5b` default in the exact
same Qwen2.5 family, drops from 8/10 clean (1.5B) to a flat **0/10** on the
strict schema - every one of the ten cases comes back `SERVER_REJECTED`.

Both `schema_hardening_eval.py` and `schema-hardening-eval` (C#) print the
actual rejected arguments and the MCP server's own validation message on
every `SERVER_REJECTED` (added specifically to chase this down instead of
guessing). Against the Python server, that made the mechanism exact: in all
ten cases, `qwen2.5-7b` emits the nested `attendee` object with only `email`
and drops `name` entirely -
`{'attendee': {'email': 'jamie.chen@example.com'}, ...}` (that's the actual
Python `dict` repr the diagnostic prints), never the reverse, never both
fields missing, never both present. Pydantic correctly rejects
it every time (`attendee.name / Field required`).

This isn't the model failing to understand the task - it extracts and uses
the same name correctly on the loose schema's flat `attendee_name` field in
the same run (7/10 clean there; the 3 misses are all `WRONG_PRIORITY`, never
a name problem). The failure is specific to producing a *nested object*
parameter: this model build omits the first declared field of that nested
shape, ten times out of ten, regardless of how the prompt is phrased. One
exact field, one exact pattern, zero variation - that reads as a structural
quirk in how this particular model/quantization generates schema-constrained
JSON for nested objects, not as "more parameters, worse reasoning." A real
capability gap would produce scattered, varied mistakes; this is one
mistake, repeated identically.

The practical lesson for the blog: **"bigger model, same family" is not a
monotonic dial for schema compliance** - verify each build with a run like
this one. A single model can have a narrow, perfectly reproducible blind
spot (here: one field inside one nested object) that a quick one- or
two-example spot-check would very likely miss, while a ten-case sweep with
per-field classification and a "show me the rejected args" diagnostic
catches it outright, in one run.

Running the same sweep against the C# server exposed a second thing this
diagnostic is good for: not a model finding, but a bug in this repo's own
C# server - see below.

### A cross-language pitfall: "strict" wasn't equally strict

The C# sweep showed the same missing-`attendee.name` mistake as above, but
split across two categories - some cases `SERVER_REJECTED`, others
`WRONG_NAME` - for what looked like the identical failure. The
`rejected args | server said` diagnostic on the ones that *did* get
rejected showed a generic `An error occurred invoking 'book_appointment_strict'`
rather than Pydantic's specific `Field required`, which was the first clue
something other than the model was different here.

Verified live against the running server: with `Attendee` declared as a
plain positional record (`Attendee(string Name, string Email)`), calling
`book_appointment_strict` with `attendee: {"email": "..."}` and no `name`
at all **succeeded** - `IsError=false`, a meeting silently booked with a
blank name. The schema handed to the model correctly listed `name` as
required, but nothing at the server's argument-binding layer actually
enforced that. Pydantic enforces required fields by default, so Python
rejects the identical payload every time; System.Text.Json's default
handling of a record's constructor parameters does not - a missing property
just becomes `null`, no exception. The two "strict" schemas *looked*
identical to the model but weren't equally strict underneath, which is
exactly why `qwen2.5-1.5b` and `-7b`'s identical mistake got scored two
different ways in C#.

Fixed in `csharp/mcp-server/Program.cs` by changing `Attendee` from
positional constructor parameters to `required` init-only properties -
System.Text.Json enforces `required` members by default (since .NET 7), no
extra configuration needed, and it doesn't change what a complete, correct
call looks like (re-verified live: a call missing `name` is now rejected
with `IsError=true`, and a full payload still succeeds exactly as before;
the schema advertised to the model - descriptions and `required` list -
is unaffected).

The generalizable lesson, if you're building your own MCP server on
System.Text.Json (or really any serializer): **advertising a field as
required in the JSON Schema you hand to the model is not the same as your
server actually enforcing it.** Check what your specific
deserialization layer does with a payload that's missing a required field
- silently defaulting it, the way a plain record's constructor parameters
do here, quietly turns your "strict" schema's server-side validation into
exactly the same silent-wrong-answer failure mode the strict schema was
supposed to eliminate.

If you ran the C# sweep before this fix landed, the numbers in that run
undercount `SERVER_REJECTED` and overcount `WRONG_NAME` for any case
missing a nested required field - re-run it for numbers comparable to the
Python side. Re-running after the fix confirmed it: both `qwen2.5-1.5b` and
`qwen2.5-7b` now show a clean `SERVER_REJECTED x10` for the exact same
missing-`attendee.name` pattern documented above. This is an actual C# run
(`dotnet run -- --model qwen2.5-1.5b qwen2.5-7b qwen3.5-0.8b qwen3.5-2b
qwen3.5-4b`), not the illustrative shape above:

```
Backend                | Schema | Clean calls  | Errors
------------------------------------------------------------------------------------------
local (qwen2.5-1.5b)   | loose  | 9/10         | WRONG_PRIORITY x1
local (qwen2.5-1.5b)   | strict | 0/10         | SERVER_REJECTED x10
local (qwen2.5-7b)     | loose  | 7/10         | WRONG_PRIORITY x3
local (qwen2.5-7b)     | strict | 0/10         | SERVER_REJECTED x10
local (qwen3.5-0.8b)   | loose  | 7/10         | WRONG_EMAIL x2, WRONG_PRIORITY x1
local (qwen3.5-0.8b)   | strict | 8/10         | SERVER_REJECTED x2
local (qwen3.5-2b)     | loose  | 7/10         | WRONG_EMAIL x1, WRONG_PRIORITY x2
local (qwen3.5-2b)     | strict | 8/10         | WRONG_EMAIL x1, SERVER_REJECTED x1
local (qwen3.5-4b)     | loose  | 8/10         | WRONG_PRIORITY x2
local (qwen3.5-4b)     | strict | 9/10         | SERVER_REJECTED x1
```

(No cloud row - `--compare-cloud` is Python-only, see above.) That re-run
also surfaced a difference from the Python results worth
flagging rather than smoothing over: in C#, **both** `qwen2.5-1.5b` and
`qwen2.5-7b` hit 0/10 on strict with this pattern, not just the 7B one as
on the Python side (where 1.5B scored 8/10 clean). Same model family, same
conceptual task, same underlying Foundry Local weights - different result
depending on which language's tool schema declaration the model saw. One
plausible reason: the two schemas aren't byte-identical even though they
describe the same shape - Python's server-generated schema expresses the
nested `attendee` object as Pydantic normally does (in this repo's case,
inline, but Pydantic will switch to a `$ref`/`$defs` indirection once a
model type is reused elsewhere), while C#'s hand-authored `ToolDefinition`
in `schema-hardening-eval` inlines `attendee`'s properties directly. This
is a hypothesis, not a confirmed cause - isolating it would mean testing
both schema shapes against the same model and language, which this repo
doesn't currently do - but it's a concrete, checkable thing to try if
you want to push this finding further for the blog, and a reminder that
"the same schema" across two language tracks is only as identical as you
verify it to be.

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
  Don't be surprised if the local model's strict clean-rate is *lower* than
  its loose one, dominated by `SERVER_REJECTED` - see the note below on why
  that's a real, worth-reporting finding, not a bug.
- **A `SERVER_REJECTED` cluster with the exact same shape every time** (same
  field, same wrong/missing value, across every case regardless of phrasing)
  is a different story than scattered, varied rejections - it points at a
  structural quirk in that model build rather than a general capability
  ceiling. Both language tracks print the rejected arguments and the MCP
  server's own validation message on every `SERVER_REJECTED`, so you can
  tell which one you're looking at instead of guessing - see the
  `qwen2.5-7b` example below.

## A note on "flat vs. nested" as a hardening strategy - and where it backfires

The obvious hypothesis is that a small model does better with a *simpler*
(flatter) schema, and hardening (adding structure) only helps once a model
is already capable enough to produce it. Actual runs bear out something
more specific than "hardening helps": **for a large cloud model, strict
beats loose outright** (fewer errors, sometimes zero); **for a genuinely
tiny local model like `qwen2.5-0.5b`, strict can score *worse* on raw
clean-call rate than loose** - not because hardening is the wrong idea, but
because `SERVER_REJECTED` becomes the dominant failure mode. A 0.5B model
producing a nested object, an array, and an exact-cased enum value
(`"high"`, not `"High"`) all in one call has a lot more ways to be
*syntactically* wrong than a model filling in four flat, unchecked strings
plus a duration that only has to be some integer - so it gets rejected more
often than it gets accepted, even though the loose schema's silent semantic
errors (wrong priority, wrong duration value) don't look any better once
you count them.

That's not an argument against hardening - a loud, retryable
`SERVER_REJECTED` is still a safer failure than a loose schema's silent
wrong answer sailing through as `OK`-shaped JSON with the wrong content.
But it does mean "harden your schemas" isn't a one-line win for every model
size: it trades one failure mode (quietly wrong) for another (loudly
rejected), and only a genuinely capable model turns that trade into more
*successful* calls, not just more *honest* failures. Run
`schema_hardening_eval.py --compare-cloud` and compare the loose/strict gap
on the local row against the same gap on the cloud row - if the local
strict column is dominated by `SERVER_REJECTED` while cloud strict is clean,
that's the local model's structural ceiling, not a schema design mistake.

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
