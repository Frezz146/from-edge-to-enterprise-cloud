# Part 03: Cloud Migration

Provisions a real Foundry project with Bicep, moves the tool-calling agent from
Parts 1-2 onto Foundry Agent Service's Conversations-era Agent Framework stack,
and adds a Foundry Toolbox for governed, reusable tools - the pieces Part 2
explicitly called out of scope for a local `stdio` process.

## What actually changes

- **Infrastructure**: [`infra/main.bicep`](infra/main.bicep) provisions the
  Foundry account, model deployment, project, and a user-assigned managed
  identity with RBAC - the manual "create a project" steps from Part 2's
  prerequisites become one `az deployment group create`.
- **Model**: a Foundry project model deployment name instead of a local
  alias - the download/load/unload lifecycle from Part 1 disappears, the
  service manages it.
- **Identity**: `DefaultAzureCredential` (Microsoft Entra ID) replaces
  nothing, since a purely local process never crossed an auth boundary.
- **Tools**: still plain functions with type hints/docstrings (Python) or
  `[Description]` attributes (C#) - Agent Framework inspects them to build the
  same shape of JSON schema an MCP tool exposes.
- **The tool-calling loop**: gone. Agent Framework's `agent.run()` /
  `RunAsync()` invokes your functions and continues the conversation
  automatically, instead of the hand-written loop in `mcp_client.py`.
- **New: a Foundry Toolbox.** [`create_toolbox.py`](python/create_toolbox.py)
  registers Code Interpreter and Web Search behind one governed, versioned,
  MCP-compatible endpoint. `cloud_agent.py` attaches it to the agent the same
  way it attaches its own Python functions, through `FoundryToolbox` - not
  through a separate integration per tool.
- **New: server-managed conversations.** `agent.create_session()` /
  `CreateSessionAsync()` returns an object whose only meaningful content is an
  opaque ID (`session.service_session_id` in Python,
  `session.ConversationId` in C#). The application stores that ID; Foundry
  stores the actual message history.
- **New: declarative, versioned agent definitions.** `cloud_agent.py` converts
  its in-process `Agent` into a `PromptAgentDefinition` with
  `to_prompt_agent()` (marked experimental in the SDK as of this writing) and
  publishes it with `client.agents.create_version()` - the same definition
  Foundry could serve as a standalone hosted agent.

## What this part deliberately does not do

- **No `FoundryAgent` for the live run.** `agent_framework.foundry.FoundryAgent`
  is the class the SDK's own docstring calls "the recommended class for
  production use" - it connects to an already-published, named/versioned
  agent (`agent_name`/`agent_version`) instead of running an in-process
  definition. Passing this part's function tools to it, at construction or at
  `run()`, fails against the live service with a real SDK bug:

  ```
  openai.BadRequestError: Error code: 400 - {'error': {'message': "Invalid
  schema for function 'get_weather': In context=(), 'additionalProperties' is
  required to be supplied and to be false.", 'type': 'invalid_request_error',
  'param': 'tools[0].parameters', 'code': 'invalid_function_parameters'}}
  ```

  `FoundryAgent`'s tool-schema generator does not set
  `additionalProperties: false`, which the strict-mode Responses API requires;
  the generic `Agent` class wrapping `FoundryChatClient` (what `cloud_agent.py`
  actually runs) builds a schema that does not hit this. `to_prompt_agent()` +
  `client.agents.create_version()` still publish the exact same definition
  declaratively either way; it is specifically *reconnecting to that published
  version through `FoundryAgent` to run it* that is broken in this SDK preview,
  not the publish step itself.
- **No self-hosted MCP server in the toolbox.** `mcp_server.py` from Part 2
  runs over `stdio` only; wiring it into a toolbox needs an HTTP(S)-reachable
  deployment (e.g. a container app), which is out of scope here. The toolbox
  uses Foundry's built-in tools instead. See [Connect agents to MCP server
  endpoints](https://learn.microsoft.com/azure/foundry/agents/how-to/tools/model-context-protocol)
  for the self-hosted path.
- **No hosted (server-executed) MCP toolbox tool.** `FoundryChatClient.get_mcp_tool()`
  can register a toolbox as a tool the *service* calls directly, which is the
  form a declaratively published agent can actually reference. In this SDK
  preview that path needs a Foundry connection resource for server-to-server
  auth to the toolbox endpoint; the local, client-executed `FoundryToolbox`
  used here works immediately with just your own Entra credential, so the
  toolbox is attached per-call rather than baked into the published
  definition. `cloud_agent.py` has the exact error this hits if you try it
  the other way.

## Prerequisites

- An Azure subscription and `az login`.
- Python 3.10+ (tested against 3.14) or .NET 8+ for the language track you run.

## Provision the infrastructure

```bash
cd part03-cloud-migration/infra
az deployment group create \
  --resource-group <your-resource-group> \
  --template-file main.bicep \
  --parameters main.bicepparam
```

This creates a Foundry `AIServices` account with project management enabled,
one project, one model deployment (`gpt-5-mini` by default), a user-assigned
managed identity, and a **Foundry User** role assignment for that identity on
the project.

Grant yourself access too - the auto-grant that happens when you create a
project through the Foundry portal UI does not apply to CLI/Bicep deployments:

```bash
PROJECT_ID=$(az deployment group show \
  --resource-group <your-resource-group> \
  --name main \
  --query properties.outputs.projectResourceId.value -o tsv)

az role assignment create \
  --role "53ca6127-db72-4b80-b1b0-d745d6d5456d" \
  --assignee-object-id "$(az ad signed-in-user show --query id -o tsv)" \
  --assignee-principal-type User \
  --scope "$PROJECT_ID"
```

That role ID is **Foundry User** (renamed from "Azure AI User" - the ID is
stable across the rename, see [RBAC for Microsoft
Foundry](https://learn.microsoft.com/azure/foundry/concepts/rbac-foundry)).
Do **not** use the "Azure AI Developer" role here: despite the name, it is
scoped to Azure Machine Learning workspaces and Foundry hubs, not Foundry
projects or agents - Microsoft's own docs call this out explicitly.

Then register the toolbox:

```bash
cd ../python
export FOUNDRY_PROJECT_ENDPOINT="<projectEndpoint output from the deployment>"
export TOOLBOX_NAME="part03-tools"
python create_toolbox.py
```

## Run it

### Python

```bash
cd part03-cloud-migration/python
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt

export FOUNDRY_PROJECT_ENDPOINT="https://<account>.services.ai.azure.com/api/projects/<project>"
export FOUNDRY_MODEL="gpt-5-mini"
export TOOLBOX_NAME="part03-tools"
python cloud_agent.py
```

### C#

```bash
cd part03-cloud-migration/csharp/cloud-agent
export ProjectEndpoint="https://<account>.services.ai.azure.com/api/projects/<project>"
export ModelDeploymentName="gpt-5-mini"
dotnet run
```

Both samples wire up the same `get_weather`/`calculate` function tools and run
two turns on a server-managed session, printing the server-side conversation
ID between them. The Python sample additionally demonstrates the Foundry
Toolbox and the declarative agent-publishing step - see "What this part
deliberately does not do" for why that stays Python-only here.

## Tear down

`rgalexbicep` (or whichever resource group you used) may hold unrelated
resources - delete only what this deployment created, not the resource group:

```bash
az cognitiveservices account delete -n <accountName> -g <your-resource-group>
az identity delete -n <identityName> -g <your-resource-group>
```

## Files in this part

```text
part03-cloud-migration/
├── blog-part03-cloud-migration.md   Published-article source, written from the real deployment below
├── part3-architecture-diagram.png   Architecture diagram referenced by the article
├── infra/
│   ├── main.bicep          AIServices account, model deployment, project, managed identity, RBAC
│   └── main.bicepparam     Concrete parameter values used for the deployment
├── python/
│   ├── requirements.txt    agent-framework-foundry, agent-framework-foundry-hosting, azure-identity
│   ├── create_toolbox.py   One-time control-plane step: registers the Foundry Toolbox
│   └── cloud_agent.py      Agent Framework agent: function tools + toolbox + sessions + declarative publish
└── csharp/
    └── cloud-agent/
        ├── cloud-agent.csproj   Microsoft.Agents.AI.AzureAI, Azure.Identity
        └── Program.cs           AsAIAgent()/RunAsync() hosted agent with the same function tools + sessions
```
