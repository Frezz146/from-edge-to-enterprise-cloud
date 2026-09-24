# Part 04: Observability and Evaluations

Parts 1 to 3 built an agent and moved it from a laptop into Microsoft Foundry.
This part answers the question that comes right after: **does it keep working
when the model, the prompt or a tool schema changes?**

It adds three things, each built on code the earlier parts already ship:

1. **Infrastructure** ([`infra/main.bicep`](infra/main.bicep)): Log Analytics,
   Application Insights with key based ingestion switched off, the Foundry
   Application Insights connection, GitHub OIDC federated credentials and
   least privilege role assignments. Part 3's resources are referenced, never
   recreated.
2. **A quality gate** over a golden dataset: the ten booking scenarios from the
   Part 2 schema hardening study, frozen as data in
   [`golden_dataset.json`](golden_dataset.json). It runs against a local
   Foundry Local model or the Part 3 cloud deployment, classifies every call
   with the Part 2 error taxonomy and can add an LLM judge on top. It fails the
   build when quality drops below the thresholds in the dataset.
3. **Tracing** across the hybrid stack: an Aspire Dashboard on your machine for
   the inner loop, Application Insights in Azure for the outer loop, with
   message content off by default.

## How the gate works

For each golden case:

1. The query and the `book_appointment_strict` schema go to the model under
   test.
2. The returned tool call is executed against **Part 2's own MCP server**, so
   `SERVER_REJECTED` means exactly what it meant in Part 2: the server's
   validation refused the arguments before the handler ran.
3. The outcome is classified with **Part 2's taxonomy** (`OK`, `NO_TOOL_CALL`,
   `WRONG_TOOL`, `MALFORMED_JSON`, `SERVER_REJECTED`, `WRONG_NAME`,
   `WRONG_EMAIL`, `WRONG_PRIORITY`, `WRONG_DURATION`, `MISSING_TOPIC`). This
   step is deterministic, needs no model and costs nothing.
4. With `--judge`, the call is also scored by `ToolCallAccuracyEvaluator`.

Nothing from Part 2 is copied. The Python gate imports the MCP server, the
classifier, the tag format fallback parser and the model loader from
`part02-mcp-orchestration/python`, so the gate and the study cannot drift
apart. The C# gate spawns Part 2's C# MCP server the same way.

The thresholds live next to the data:

| Setting | Meaning |
| --- | --- |
| `min_clean_call_rate` | Share of cases classified `OK` (both tracks) |
| `min_tool_call_accuracy` | Mean judge score on Python's 1 to 5 scale |
| `min_judge_pass_rate` | Share of calls the .NET judge rates accurate (it returns a boolean, not a score) |

A case without a parseable tool call counts as the judge's floor (1 in
Python, inaccurate in .NET), so a model that stops calling tools cannot lift
the average.

## Prerequisites

- Parts 1 to 3 working, in particular the Part 3 deployment in your resource
  group.
- `az login`.
- Python 3.11 or newer (verified with 3.14) or the .NET 8 SDK or newer (verified
  with .NET 10) for the language track you run. The C# projects target net8.0.
  `eval-runner` and the Part 2 `mcp-server` it starts set `RollForward=Major`,
  so they also run on a machine that only has a newer runtime.

## Provision the infrastructure

```bash
cd part04-observability-evals/infra
az deployment group create \
  --resource-group <your-resource-group> \
  --template-file main.bicep \
  --parameters main.bicepparam \
  --parameters developerPrincipalId=$(az ad signed-in-user show --query id -o tsv)
```

`namePrefix` must match Part 3. The template derives the Part 3 account and
identity names with the same `uniqueString()` formula, so there is nothing to
copy by hand.

What each identity gets and why:

| Identity | Role | Scope | Why |
| --- | --- | --- | --- |
| Part 3 agent identity (also used by CI) | Monitoring Metrics Publisher | Application Insights | Write spans with an Entra ID token |
| Part 3 agent identity | Cognitive Services OpenAI User | Foundry account | Cloud gate and LLM judge |
| Project managed identity | Monitoring Metrics Publisher, Log Analytics Reader, Privileged Monitoring Data Reader | Application Insights | Foundry's own server side traces and evaluations |
| You (optional) | Monitoring Metrics Publisher, Log Analytics Reader | Application Insights | Run locally and browse traces |
| You (optional) | Cognitive Services OpenAI User | Foundry account | Run the cloud gate locally |

The CI identity can write telemetry but not read it. A pipeline that only
writes has no reason to see production traces.

### A note on the connection and `DisableLocalAuth`

The Foundry connection stores the Application Insights connection string as
its key. That is the shape Microsoft's own
[foundry-samples template](https://github.com/microsoft-foundry/foundry-samples/blob/main/infrastructure/infrastructure-setup-bicep/01-connections/connection-application-insights.bicep)
uses. It is also the only shape `azure-ai-projects` accepts when a client asks
the project for its connection string. With `DisableLocalAuth: true` that
string is an address, not a credential: ingestion is rejected without an Entra
ID token. If you publish agents that Foundry runs server side (prompt or hosted
agents) and want their traces with local auth disabled, switch the connection's
auth type to *Project managed identity* as described in
[Configure Microsoft Entra authentication for Foundry Agent trace ingestion](https://learn.microsoft.com/azure/foundry/observability/how-to/trace-ingestion-entra-authentication)
(preview). The template already grants the project identity the role it needs.
Set `disableLocalAuth=false` if you prefer to skip that preview path entirely.

## Run the gate locally

### Python

```bash
cd part04-observability-evals/python
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt

python eval_pipeline.py                     # local gate, qwen2.5-1.5b
```

### C#

```bash
cd part04-observability-evals/csharp/eval-runner
dotnet run                                  # local gate, qwen3.5-4b
dotnet run -- --model qwen2.5-1.5b          # the cross-language finding below
```

The C# runner defaults to `qwen3.5-4b`, not `qwen2.5-1.5b`. With the
hand-authored C# tool schema from Part 2, `qwen2.5-1.5b` drops `attendee.name`
in every case and scores 0/10, while the same model scores 8/10 against the
Python server's schema. See [Verified results](#verified-results).

Both print one line per case plus a summary and exit `1` when the gate fails.
Python also writes `evaluation_results.json` (git ignored) and, inside GitHub
Actions, a table on the run's summary page.

### Reproduce the Part 2 regression

Part 2 found that `qwen2.5-7b`, the bigger sibling of `qwen2.5-1.5b`, drops
`attendee.name` on every strict call. Point the gate at it:

```bash
python eval_pipeline.py --model qwen2.5-7b
```

Real output (server messages shortened):

```
=== local (qwen2.5-7b) | 10 golden cases | tool book_appointment_strict ===
  [case_01_explicit] SERVER_REJECTED
    server said: ... 1 validation error: attendee.name Field required [type=missing, input_value={'email': 'jamie.chen@example.com'}, input_type=dict]
  ...
  [case_08_standard_priority] SERVER_REJECTED
    server said: ... 2 validation errors: attendee.name Field required [...] priority Input should be 'low', 'medium' or 'high' [type=literal_error, input_value='standard', input_type=str]
  ...
Backend                        | Clean calls | Judge   | Errors
local (qwen2.5-7b)             | 0/10        | -       | SERVER_REJECTED x10
  clean call rate 0% (min 80%)

Quality gate FAILED.
```

That is the pull request the gate exists to stop: "bigger model, same family"
looks like a safe upgrade and silently breaks one nested field. Your numbers
depend on your hardware and the exact model build; run it yourself.

## Cloud gate and LLM judge

```bash
export AZURE_OPENAI_ENDPOINT="<openAiEndpoint output from the deployment>"
export FOUNDRY_MODEL="gpt-5-mini"   # the Part 3 deployment under test
export JUDGE_MODEL="gpt-5-mini"     # the judge

python eval_pipeline.py --backend cloud --judge
dotnet run -- --judge                # C#: local model under test, cloud judge
```

Authentication is `DefaultAzureCredential` everywhere: your `az login`
locally, the OIDC federated credential in CI. There is no API key in this part.

Three things to know about the judge:

- It is an LLM. Scores vary slightly between runs and every call costs tokens.
  That is why the deterministic taxonomy is the primary gate and the judge
  only a second opinion. It is also why thresholds apply to averages over the
  dataset rather than to single cases.
- `gpt-5` family and o-series deployments are reasoning models that reject the
  sampling settings in the evaluators' prompts. Python passes
  `is_reasoning_model=True` automatically; C# strips those settings in a small
  `DelegatingChatClient` (`ReasoningModelCompatibleChatClient`).
- Ideally the judge is not the model under test. With a single deployment, as
  in this demo, it is; add a second deployment for the judge in a real setup.
- Capacity matters. Part 3 deploys `gpt-5-mini` with 10,000 tokens per minute.
  A reasoning judge uses a few thousand tokens per call, so running the cloud
  gate and a judged local run back to back produced `429` rate limit responses.
  The SDK retried them and the run finished, but raise `modelCapacity` in Part 3
  or give the judge its own deployment if runs start failing.

Two SDK details the Python judge handles for you (both found while verifying
this part, see [Verified results](#verified-results)):

- `azure-ai-evaluation` 1.18 rejects its own `AzureOpenAIModelConfiguration`
  when the `credential` key is set ("Model config validation failed"). The
  credential is passed as the evaluator's `credential` argument instead.
- The evaluators default to `api_version` `2024-02-15-preview`, older than any
  reasoning model. `eval_pipeline.py` sets `2025-04-01-preview`.

## Verified results

Everything in this part was run end to end on 24 September 2026 against the
Part 3 deployment (Sweden Central, `gpt-5-mini`, GlobalStandard with 10,000
tokens per minute) from a MacBook with Foundry Local on the WebGPU execution
provider, Python 3.14 and the .NET 10 runtime.

| Track | Model under test | Clean calls | Judge | What went wrong |
| --- | --- | --- | --- | --- |
| Python, local | `qwen2.5-1.5b` | 8/10 | 4.40/5 | Invented priorities `normal` ("no-rush") and `standard` ("standard-priority"), both rejected by the enum |
| Python, local | `qwen2.5-7b` | 0/10 | not run | `attendee.name` missing in every call (the Part 2 regression) |
| Python, cloud | `gpt-5-mini` | 10/10 | 5.00/5 | Nothing |
| C#, local | `qwen2.5-1.5b` | 0/10 | 0 of 10 accurate | `attendee.name` missing in every call, `durationMinutes` missing in three, same invented priorities |
| C#, local | `qwen3.5-4b` | 9/10 | 8 and 9 of 10 accurate in two runs | Invented priority `standard` |

What these numbers show:

- **The gate and the judge agree where it matters.** In Python the judge gave
  5.0 to every clean call and 2.0 to both rejected ones.
- **The judge is not deterministic.** Two C# runs with the same model at temperature 0 got
  8 and 9 accurate verdicts: `case_07_no_rush` flipped between runs. That is
  why the deterministic taxonomy is the gate and the judge a second opinion.
- **The Part 2 cross-language finding reproduces.** The same `qwen2.5-1.5b`
  weights score 8/10 against the Python server's Pydantic schema and 0/10
  against the hand-authored C# schema, always dropping the first field of the
  nested `attendee` object. Part 2 left the cause as an open hypothesis (schema
  presentation); Part 4 confirms the effect with a different harness. The C#
  runner therefore defaults to `qwen3.5-4b`, which matches its Part 2 score of
  9/10.
- **Traces arrive in both loops.** `traced_agent.py` exported to Application
  Insights with Entra ID only ingestion and to a local Aspire Dashboard;
  `eval_pipeline.py --trace otlp` did the same for the gate.

Problems found and fixed while verifying:

| Symptom | Cause | Fix |
| --- | --- | --- |
| `Model config validation failed` when building the Python judge | `azure-ai-evaluation` 1.18 rejects its own config type when it contains `credential` | Pass the credential as the evaluator's `credential` argument |
| (preventive) judge on an old API version | The evaluators default to `api_version` 2024-02-15-preview | `eval_pipeline.py` sets 2025-04-01-preview |
| `MSB4025 An XML comment cannot contain '--'` | A comment in the `.csproj` mentioned the `--judge` flag | Reworded |
| `error OPENAI001` in the C# build | The `ChatClient` constructor that takes an `AuthenticationPolicy` (Entra ID auth) is experimental | `OPENAI001` added to `NoWarn`, next to `AIEVAL001` |
| `You must install or update .NET to run this application` | net8.0 apps on a machine with only the .NET 10 runtime | `RollForward=Major` in `eval-runner.csproj` and in the Part 2 `mcp-server.csproj` that it spawns |
| C# server only says `An error occurred invoking ...` | The C# MCP server hides the validation detail | The runner prints the rejected arguments plus the judge's reason when it says inaccurate |
| `429` rate limit responses from the judge | Gate, cloud backend and judge share one 10,000 TPM deployment | Retried automatically by the SDK; raise capacity or split the judge deployment |

## Tracing across the hybrid stack

Two levels, as in Microsoft Foundry's own guidance:

- **Server side**: agents that Foundry runs (published prompt agents, hosted
  agents) are traced by Foundry itself once the Application Insights connection
  exists. No code.
- **Client side**: code that runs in your process, like the Part 3 agent's
  `agent.run()` loop or this gate, is traced with OpenTelemetry. Agent
  Framework already emits spans with the GenAI semantic conventions; you only
  choose the destination.

### Inner loop: Aspire Dashboard on your machine

```bash
docker run --rm -it -d -p 18888:18888 -p 4317:18889 -p 4318:18890 \
  -e ASPIRE_DASHBOARD_UNSECURED_ALLOW_ANONYMOUS=true \
  --name aspire-dashboard mcr.microsoft.com/dotnet/aspire-dashboard:latest

python eval_pipeline.py --trace otlp        # gate spans, one per case
python traced_agent.py --target otlp        # the Part 3 agent, fully traced
```

Open http://localhost:18888. It costs nothing, needs no Azure apart from the
model calls and shows the same OpenTelemetry data you get in Azure.

### Outer loop: Application Insights

```bash
export FOUNDRY_PROJECT_ENDPOINT="https://<account>.services.ai.azure.com/api/projects/<project>"
export FOUNDRY_MODEL="gpt-5-mini"

python traced_agent.py --target azure
python eval_pipeline.py --backend cloud --judge --trace azure
```

Neither script is given a connection string. `traced_agent.py` calls
`FoundryChatClient.configure_azure_monitor()`, `eval_pipeline.py` asks the
project through `azure-ai-projects`; both read the connection the Bicep
template created and export with an Entra ID token. Both print the trace ID
they produced, so you can jump straight to it in Transaction search.

### Data privacy and content recording

Spans can carry prompts, responses and tool arguments. For an enterprise
audience, especially in the EU, that is a decision, not a default:

| Stack | Switch | Default |
| --- | --- | --- |
| Agent Framework (`traced_agent.py`) | `--sensitive` or `ENABLE_SENSITIVE_DATA=true` | off |
| Azure AI SDK instrumentors (`azure-ai-projects`, `azure-ai-inference`) | `AZURE_TRACING_GEN_AI_CONTENT_RECORDING_ENABLED=true` | off |
| This gate (`eval_pipeline.py`) | none, it never records content | off |

The gate's spans carry case IDs, outcomes and scores only. Keep content
recording for dev and staging projects where storing message content in
telemetry has been agreed on. Also remember who can read it: Log Analytics
Reader on the Application Insights resource or Privileged Monitoring Data
Reader when the underlying tables are protected.

## CI/CD

[`.github/workflows/eval.yml`](../.github/workflows/eval.yml) runs two jobs on
every pull request that touches Part 2's Python track, this part or the
workflow itself:

- **local-gate**: Foundry Local on the runner, deterministic taxonomy only. No
  Azure, no secrets, so it also runs for forks. The model under test is the
  `FOUNDRY_LOCAL_MODEL` line in the workflow; a pull request that changes it
  is exactly what this gate checks.
- **cloud-gate**: signs in through OIDC, runs the Part 3 deployment with the
  LLM judge and sends its spans to Application Insights. It is skipped until
  the repository variables below exist.

After deploying the infrastructure, set these **repository variables**
(Settings, Secrets and variables, Actions, Variables). They are IDs and URLs,
not secrets, which is the point:

| Variable | Source |
| --- | --- |
| `AZURE_CLIENT_ID` | output `azureClientId` |
| `AZURE_TENANT_ID` | output `azureTenantId` |
| `AZURE_SUBSCRIPTION_ID` | output `azureSubscriptionId` |
| `AZURE_OPENAI_ENDPOINT` | output `openAiEndpoint` |
| `FOUNDRY_PROJECT_ENDPOINT` | Part 3 output `projectEndpoint` |

Then protect `main` with a branch rule that requires the `local-gate` (and, once
configured, `cloud-gate`) checks.

## What this part deliberately does not do

- **No groundedness gate.** The Part 3 agent answers from tool results, not
  from a retrieval index. The golden cases are tool calls, so tool call
  accuracy is the metric that matches. `GroundednessEvaluator` becomes useful
  once the agent grounds answers in Toolbox output such as web search, with
  the retrieved text passed as context.
- **No continuous evaluation of production traffic.** Sampling live
  conversations, scoring them on a schedule and alerting on a falling average
  is the next step. Foundry offers it for agents it runs. It needs real
  traffic to be meaningful, which a demo repository does not have.
- **No Foundry Local model cache in CI.** Every `local-gate` run downloads the
  model. A self hosted runner keeps the cache between runs.

## Tear down

Delete only what this part created; the resource group may hold other things:

```bash
az monitor app-insights component delete -a <appInsightsName> -g <your-resource-group>
az monitor log-analytics workspace delete -n <workspaceName> -g <your-resource-group> --yes
az identity federated-credential delete --identity-name <namePrefix>-agent-identity -g <your-resource-group> -n github-pull-request --yes
az identity federated-credential delete --identity-name <namePrefix>-agent-identity -g <your-resource-group> -n github-main --yes
```

Deleting the Part 3 account (see its README) removes the connections and the
account level role assignments with it.

## Files in this part

```text
part04-observability-evals/
├── README.md
├── golden_dataset.json          the ten Part 2 cases as data, plus the gate thresholds (shared by both tracks)
├── infra/
│   ├── main.bicep               monitoring, Foundry connection, OIDC credentials, RBAC on top of Part 3
│   └── main.bicepparam          parameters; namePrefix must match Part 3
├── python/
│   ├── requirements.txt         foundry-local-sdk, mcp, openai, azure-ai-evaluation, OpenTelemetry, agent-framework
│   ├── eval_pipeline.py         the quality gate: local or cloud backend, Part 2 taxonomy, optional LLM judge
│   ├── telemetry.py             one switch for the gate's tracing: none, console, otlp, azure
│   └── traced_agent.py          the Part 3 agent with Agent Framework tracing to Aspire or Application Insights
└── csharp/
    └── eval-runner/
        ├── eval-runner.csproj   Foundry Local, ModelContextProtocol, Microsoft.Extensions.AI.Evaluation.Quality
        └── Program.cs           the quality gate in C#: local model (default qwen3.5-4b), Part 2 C# MCP server, optional .NET judge
```

`golden_dataset.json` sits at the part root, not inside a language folder,
because both tracks read it. The C# runner maps its snake_case expectations to
the C# server's camelCase arguments; the Python runner uses them as they are.
The workflow lives in the repository root under `.github/workflows/`, where
GitHub expects it.
