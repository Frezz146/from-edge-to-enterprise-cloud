# Part 02: MCP Orchestration

Connects the local model from Part 1 to external tools through the Model
Context Protocol (MCP), and compares how reliably a compact SLM calls those
tools versus a large cloud model, given the exact same tool schema.

## Why MCP here

MCP separates "what a tool looks like" from "which model is calling it". The
server in `mcp_server.py` exposes two tools (`get_weather`, `calculate`) with
a JSON schema; the client in `mcp_client.py` discovers that schema at runtime
and hands it, unchanged, to whichever chat backend is calling - a 0.5B local
model or a frontier model in the cloud.

## Run it

### Python

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
backend.

### C#

```bash
cd part02-mcp-orchestration/csharp/mcp-agent
dotnet run
```

The C# client connects to the same Python MCP server over stdio (a concrete
demonstration that MCP doesn't care what language either side is written
in), then runs one tool-calling turn against the local model using a
hand-declared tool schema mirroring `get_weather()`.

## What to look for

Small models are more likely to answer from parametric knowledge instead of
calling the tool, or to call it with malformed arguments. If you see that in
the `--compare-cloud` output, that's the tool-schema hardening problem this
part's blog post is about - clearer descriptions, tighter JSON schemas
(enums instead of free text, required fields), and forcing `tool_choice`
where appropriate all help close the gap.
