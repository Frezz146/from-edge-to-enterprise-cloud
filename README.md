# From Edge to Enterprise Cloud

> A four-part code companion for building hybrid AI agents, starting with local inference and moving toward production on Azure.

This repository accompanies the blog series **From Edge to Enterprise Cloud: Hybrid Agent Architecture with Foundry Local and Azure AI Foundry**, published on [blog.frezz-tech.de](https://blog.frezz-tech.de/). It follows one practical path from a model running on a developer machine to an agent architecture that can be orchestrated, migrated, observed, and evaluated in the cloud.

The examples are intentionally incremental. Each part adds one architectural capability while keeping the previous step understandable and independently runnable.

## The journey

1. **Local development** - Run a small language model in-process with the Foundry Local GA SDK.
2. **MCP orchestration** - Connect the model to tools through the Model Context Protocol, then measure how much a hardened JSON schema closes the tool-call error gap between a compact local model and a cloud frontier model.
3. **Cloud migration** - Move selected workloads to the Foundry Agent Service with `AIProjectClient`.
4. **Observability and evaluations** - Automate quality checks with groundedness evaluation in CI/CD.

## Start here

### Part 01 - Local development

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

The first run downloads execution providers and the selected model. Later runs reuse the local cache. See [part01-local-development/README.md](part01-local-development/README.md) for the complete local inference lifecycle and the local-vs-cloud benchmark.

### Part 02 - MCP orchestration

```bash
cd part02-mcp-orchestration/python
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python mcp_client.py
# python schema_hardening_eval.py   # the actual loose-vs-strict schema study
```

See [part02-mcp-orchestration/README.md](part02-mcp-orchestration/README.md) for the C# samples and the schema hardening study - a matched pair of tools (same task, different schema rigor) run through an error taxonomy, not just a pass/fail check. Python and C# each ship their own independent MCP server, so neither language track needs the other installed to run.

### Part 03 - Cloud migration

Requires a Microsoft Foundry project with a deployed model. See
[part03-cloud-migration/README.md](part03-cloud-migration/README.md) for setup and the Python/C# quickstarts.

### Part 04 - Observability and evaluations

```bash
cd part04-observability-evals/python
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python eval_pipeline.py
```

See [part04-observability-evals/README.md](part04-observability-evals/README.md) and [.github/workflows/eval.yml](.github/workflows/eval.yml) for the CI/CD gate.

## Repository structure

```text
from-edge-to-enterprise-cloud/
├── README.md
├── .github/
│   └── workflows/
│       └── eval.yml
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
│   ├── python/
│   │   ├── requirements.txt
│   │   └── cloud_agent.py
│   └── csharp/
│       └── cloud-agent/
│           ├── cloud-agent.csproj
│           └── Program.cs
├── part04-observability-evals/
│   ├── README.md
│   └── python/
│       ├── requirements.txt
│       ├── eval_pipeline.py
│       └── golden_dataset.json
├── .gitignore
└── LICENSE
```

Every C# sample lives in its own named subfolder under `csharp/` (`csharp/<project-name>/<project-name>.csproj`), even where a part only has one project - the .NET SDK glob-includes every `.cs` file under a project's directory by default, so two top-level-statement programs can't share a folder without colliding. One project per folder keeps every part's C# layout identical, whether it has one runnable sample or two.

All four parts are implemented end to end, in both Python and C#. Each part's
own README covers prerequisites, setup and what to look for when you run it.

## Technology focus

- **Foundry Local** for developer-local model discovery, caching, and inference
- **Python and C#** for application and agent implementations
- **MCP** for explicit tool and capability boundaries
- **Foundry Agent Service** for hosted agent workflows
- **Azure AI Foundry evaluations** for automated quality checks

## Project principles

- **Local first** - Keep the feedback loop fast and make experimentation possible without a remote endpoint.
- **Portable by design** - Separate model interaction, tools, and deployment concerns so they can evolve independently.
- **Observable progress** - Make downloads, model loading, inference, and cleanup visible during development.
- **Production minded** - Treat evaluation, repeatability, and clear boundaries as part of the architecture from the beginning.

## Official resources

- [Foundry Local on GitHub](https://github.com/microsoft/Foundry-Local)
- [Foundry Local Python SDK on PyPI](https://pypi.org/project/foundry-local-sdk/)
- [Foundry Local C# SDK on NuGet](https://www.nuget.org/packages/Microsoft.AI.Foundry.Local)
- [Foundry Local tool calling](https://learn.microsoft.com/azure/foundry-local/how-to/how-to-use-tool-calling-with-foundry-local)
- [Model Context Protocol](https://modelcontextprotocol.io/)
- [Azure AI Agents client library](https://learn.microsoft.com/python/api/overview/azure/ai-agents-readme)
- [Connect agents to MCP server endpoints](https://learn.microsoft.com/azure/foundry/agents/how-to/tools/model-context-protocol)
- [Azure AI Evaluation client library](https://learn.microsoft.com/python/api/overview/azure/ai-evaluation-readme)
- [Microsoft Foundry documentation](https://learn.microsoft.com/azure/ai-foundry/)

## License

See [LICENSE](LICENSE) for the project license.
