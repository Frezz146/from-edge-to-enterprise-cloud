# Part 03: Cloud Migration

Moves the tool-calling agent from Parts 1-2 into Foundry Agent Service, a
managed, hosted runtime, and shows exactly what has to change to get there.

## What actually changes

- **Model**: a Foundry project model deployment name instead of a local
  alias - the download/load/unload lifecycle from Part 1 disappears, the
  service manages it.
- **Identity**: `DefaultAzureCredential` (Microsoft Entra ID) replaces
  nothing, since a purely local process never crossed an auth boundary.
- **Tools**: still plain functions with type hints and docstrings - the SDK
  inspects them to build the same shape of JSON schema an MCP tool exposes.
- **The tool-calling loop**: gone. `enable_auto_function_calls` (Python) or
  the run-polling loop (C#) lets the SDK invoke your functions and continue
  the conversation automatically, instead of the hand-written loop in
  `mcp_client.py`.

Foundry Agent Service can also call a remote MCP server directly through the
built-in MCP tool, which would let a hosted agent reuse `mcp_server.py` from
Part 2 unchanged - provided it's reachable over HTTP(S) rather than local
stdio. See [Connect agents to MCP server endpoints](https://learn.microsoft.com/azure/foundry/agents/how-to/tools/model-context-protocol).

## Prerequisites

- A Microsoft Foundry project with a deployed chat model
  ([environment setup](https://learn.microsoft.com/azure/ai-foundry/agents/environment-setup))
- The **Foundry User** RBAC role on that project
- Signed in locally: `az login`

## Run it

### Python

```bash
cd part03-cloud-migration/python
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt

export PROJECT_ENDPOINT="https://<account>.services.ai.azure.com/api/projects/<project>"
export MODEL_DEPLOYMENT_NAME="<your-model-deployment-name>"
python cloud_agent.py
```

### C#

```bash
cd part03-cloud-migration/csharp/cloud-agent
export ProjectEndpoint="https://<account>.services.ai.azure.com/api/projects/<project>"
export ModelDeploymentName="<your-model-deployment-name>"
dotnet run
```

The C# sample runs a plain instructions-only agent turn; the function-tool
wiring mirroring Part 2's tools is fully worked out in `cloud_agent.py`.

## Files in this part

```text
part03-cloud-migration/
├── python/
│   ├── requirements.txt   azure-ai-projects (pinned to the 1.x line - see the comment in the file for why), azure-ai-agents, azure-identity
│   └── cloud_agent.py      AIProjectClient + AgentsClient hosted agent, function tools mirroring Part 2
└── csharp/
    └── cloud-agent/
        ├── cloud-agent.csproj   Azure.AI.Agents.Persistent, Azure.Identity
        └── Program.cs           Azure.AI.Agents.Persistent hosted agent (instructions-only, see above)
```

There's only one project per language in this part - no separate quickstart
and deep-dive split like Parts 1 and 2, since a hosted agent run has less
surface area to demo in stages: create the agent, run it, read the answer,
clean up. `cloud_agent.py` is the fuller of the two (it also wires up
function tools); the C# sample intentionally stays instructions-only rather
than re-deriving the same function-tool plumbing in a different SDK - see
"What actually changes" above for why.
