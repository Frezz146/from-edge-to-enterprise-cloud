# From Edge to Enterprise Cloud

> A four-part code companion for building hybrid AI agents: from local inference on a developer machine to an observed, quality-gated agent in Microsoft Foundry.

This repository accompanies the blog series **From Edge to Enterprise Cloud: Hybrid Agent Architecture with Foundry Local and Microsoft Foundry**, published on [blog.frezz-tech.de](https://blog.frezz-tech.de/). It follows one practical path from a model running on a developer machine to an agent architecture that can be orchestrated, migrated, observed and evaluated in the cloud.

The examples are intentionally incremental. Each part adds one architectural capability while keeping the previous step understandable and independently runnable. Later parts build on earlier code instead of copying it: Part 4's quality gate imports Part 2's MCP server and error taxonomy and traces Part 3's agent unchanged.

## The journey

1. **Local development:** run a small language model in-process with the Foundry Local GA SDK.
2. **MCP orchestration:** connect the model to tools through the Model Context Protocol, then measure how much a hardened JSON schema closes the tool call error gap between a compact local model and a cloud frontier model.
3. **Cloud migration:** move the agent to Foundry Agent Service with Bicep, Microsoft Entra ID and a Foundry Toolbox.
4. **Observability and evaluations:** turn the Part 2 study into a golden dataset and a CI quality gate (deterministic taxonomy plus an optional LLM judge), provision Application Insights with Bicep and trace the hybrid stack with OpenTelemetry.

## Start here

### Part 01: Local development

```bash
cd part01-local-development/python
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python local-loop.py
# python benchmark.py   # local vs. cloud latency/throughput comparison
```

```bash
cd part01-local-development/csharp/local-loop
dotnet run
# cd ../benchmark && dotnet run   # local vs. cloud latency/throughput comparison
```

The first run downloads execution providers and the selected model. Later runs reuse the local cache. See [part01-local-development/README.md](part01-local-development/README.md) for the complete local inference lifecycle and the local vs. cloud benchmark.

### Part 02: MCP orchestration

```bash
cd part02-mcp-orchestration/python
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python mcp_client.py
# python schema_hardening_eval.py   # the actual loose vs. strict schema study
```

See [part02-mcp-orchestration/README.md](part02-mcp-orchestration/README.md) for the C# samples and the schema hardening study: a matched pair of tools (same task, different schema rigor) run through an error taxonomy instead of a pass/fail check. Python and C# each ship their own independent MCP server, so neither language track needs the other installed to run.

### Part 03: Cloud migration

```bash
cd part03-cloud-migration/infra
az deployment group create \
  --resource-group <your-resource-group> \
  --template-file main.bicep \
  --parameters main.bicepparam
```

Provisions a Microsoft Foundry project, a model deployment and a managed identity with Bicep, then registers a Foundry Toolbox and runs the Part 1 and 2 agent against it. See [part03-cloud-migration/README.md](part03-cloud-migration/README.md) for the full setup, RBAC and the Python/C# quickstarts.

### Part 04: Observability and evaluations

```bash
cd part04-observability-evals/infra
az deployment group create \
  --resource-group <your-resource-group> \
  --template-file main.bicep \
  --parameters main.bicepparam \
  --parameters developerPrincipalId=$(az ad signed-in-user show --query id -o tsv)

cd ../python
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python eval_pipeline.py                     # local quality gate, qwen2.5-1.5b
python eval_pipeline.py --model qwen2.5-7b  # reproduces the Part 2 regression: the gate fails
```

```bash
cd part04-observability-evals/csharp/eval-runner
dotnet run
```

Extends the Part 3 deployment with Log Analytics, Application Insights (Entra ID only ingestion), the Foundry Application Insights connection, GitHub OIDC federated credentials and least privilege RBAC. The quality gate runs locally for free or against the Part 3 cloud deployment with an LLM judge. See [part04-observability-evals/README.md](part04-observability-evals/README.md) for tracing with an Aspire Dashboard or Application Insights and [.github/workflows/eval.yml](.github/workflows/eval.yml) for the two CI gates.

## Repository structure

```text
from-edge-to-enterprise-cloud/
├── README.md
├── .github/
│   └── workflows/
│       └── eval.yml                  Part 4 quality gates (local and cloud)
├── part01-local-development/
│   ├── README.md
│   ├── python/
│   │   ├── requirements.txt
│   │   ├── local-loop.py
│   │   └── benchmark.py
│   └── csharp/
│       ├── local-loop/
│       │   ├── local-loop.csproj
│       │   └── Program.cs
│       └── benchmark/
│           ├── benchmark.csproj
│           └── Program.cs
├── part02-mcp-orchestration/
│   ├── README.md
│   ├── python/
│   │   ├── requirements.txt
│   │   ├── mcp_server.py
│   │   ├── mcp_client.py
│   │   └── schema_hardening_eval.py
│   └── csharp/
│       ├── mcp-server/
│       │   ├── mcp-server.csproj
│       │   └── Program.cs
│       ├── mcp-agent/
│       │   ├── mcp-agent.csproj
│       │   └── Program.cs
│       └── schema-hardening-eval/
│           ├── schema-hardening-eval.csproj
│           └── Program.cs
├── part03-cloud-migration/
│   ├── README.md
│   ├── part3-architecture-diagram.png
│   ├── infra/
│   │   ├── main.bicep
│   │   └── main.bicepparam
│   ├── python/
│   │   ├── requirements.txt
│   │   ├── create_toolbox.py
│   │   └── cloud_agent.py
│   └── csharp/
│       └── cloud-agent/
│           ├── cloud-agent.csproj
│           └── Program.cs
├── part04-observability-evals/
│   ├── README.md
│   ├── golden_dataset.json           shared by the Python and C# gate
│   ├── infra/
│   │   ├── main.bicep
│   │   └── main.bicepparam
│   ├── python/
│   │   ├── requirements.txt
│   │   ├── eval_pipeline.py
│   │   ├── telemetry.py
│   │   └── traced_agent.py
│   └── csharp/
│       └── eval-runner/
│           ├── eval-runner.csproj
│           └── Program.cs
├── Directory.Build.props             RollForward=Major for every C# project
├── .gitignore
└── LICENSE
```

All C# samples target net8.0 (LTS). `Directory.Build.props` in the repository root sets `RollForward=Major`, so they also run on a machine that only has a newer runtime such as .NET 10.

Every C# sample lives in its own named subfolder under `csharp/` (`csharp/<project-name>/<project-name>.csproj`), even where a part only has one project. The .NET SDK includes every `.cs` file under a project's directory by default, so two top-level-statement programs cannot share a folder without colliding. One project per folder keeps every part's C# layout identical.

All four parts are implemented end to end in both Python and C#. Each part's own README covers prerequisites, setup and what to look for when you run it.

## Technology focus

- **Foundry Local** for developer-local model discovery, caching and inference
- **Python and C#** for application and agent implementations
- **MCP** for explicit tool and capability boundaries
- **Foundry Agent Service** for hosted agent workflows
- **Bicep and Microsoft Entra ID** for reproducible, keyless infrastructure
- **azure-ai-evaluation and Microsoft.Extensions.AI.Evaluation** for automated quality gates
- **OpenTelemetry, Aspire Dashboard and Application Insights** for tracing

## Project principles

- **Local first:** keep the feedback loop fast and make experimentation possible without a remote endpoint.
- **Portable by design:** separate model interaction, tools and deployment concerns so they can evolve independently.
- **Observable progress:** make downloads, model loading, inference, cleanup and tool calls visible, first on the console and later as traces.
- **Production minded:** treat evaluation, repeatability and clear boundaries as part of the architecture from the beginning.
- **Measured, not assumed:** every claim in the blog series comes from a script in this repository you can rerun on your own hardware.

## Official resources

- [Foundry Local on GitHub](https://github.com/microsoft/Foundry-Local)
- [Foundry Local Python SDK on PyPI](https://pypi.org/project/foundry-local-sdk/)
- [Foundry Local C# SDK on NuGet](https://www.nuget.org/packages/Microsoft.AI.Foundry.Local)
- [Foundry Local tool calling](https://learn.microsoft.com/azure/foundry-local/how-to/how-to-use-tool-calling-with-foundry-local)
- [Model Context Protocol](https://modelcontextprotocol.io/)
- [Connect agents to MCP server endpoints](https://learn.microsoft.com/azure/foundry/agents/how-to/tools/model-context-protocol)
- [Set up tracing for AI agents in Microsoft Foundry](https://learn.microsoft.com/azure/foundry/observability/how-to/trace-agent-setup)
- [Azure AI Evaluation client library for Python](https://learn.microsoft.com/python/api/overview/azure/ai-evaluation-readme)
- [Microsoft.Extensions.AI.Evaluation libraries](https://learn.microsoft.com/dotnet/ai/evaluation/libraries)
- [Microsoft Foundry documentation](https://learn.microsoft.com/azure/ai-foundry/)

## License

See [LICENSE](LICENSE) for the project license.
