"""Connect a Foundry Local chat model to tools exposed over MCP, and run the
same tool-calling evaluation against a small local model and (optionally) a
large cloud frontier model to compare tool-call reliability.

This is the core question of Part 2: does a compact SLM running on-device
call tools as reliably as a large model in the cloud, given the exact same
tool schema? MCP is what lets both backends share that schema unchanged -
the server in mcp_server.py doesn't know or care which model is calling it.

Built directly on the raw MCP and Foundry Local SDKs rather than a higher-
level agent framework, on purpose: this part's whole point is measuring
tool-call reliability and error rates, and that means inspecting each
attempt's raw arguments and outcome. A framework that auto-executes tool
calls for you hides exactly the detail this script exists to look at - see
schema_hardening_eval.py for where that distinction really matters.

Usage:
    python mcp_client.py                 # local model only
    python mcp_client.py --compare-cloud # local model + Azure OpenAI, side by side
    python mcp_client.py --model qwen2.5-1.5b qwen2.5-7b  # sweep multiple local models
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from foundry_local_sdk import Configuration, FoundryLocalManager

DEFAULT_MODEL_ALIAS = "qwen2.5-0.5b"
APP_NAME = "frezz_tech_mcp_agent"
SERVER_SCRIPT = str(Path(__file__).parent / "mcp_server.py")

SYSTEM_PROMPT = (
    "You are a helpful assistant with access to tools. "
    "Use them when needed to answer questions accurately."
)

# Prompts chosen so that a correct answer requires at least one real tool
# call - a model that "hallucinates" the answer instead of calling the tool
# will produce a plausible-looking but wrong or unverifiable response.
EVAL_PROMPTS = [
    "What is the weather in Berlin right now?",
    "What is 137 multiplied by 26?",
    "What's the weather in Tokyo, and what is 42 * 17?",
    "What is the current weather in Paris?",
    "Calculate 256 divided by 8, then add 19.",
    "What's the weather like in Sydney today?",
    "What is (12 + 8) * 3?",
    "Tell me the weather in New York and what 99 minus 47 is.",
    "What is 15 percent of 240?",
    "What's the weather in Berlin, Tokyo, and Paris?",
]


def mcp_tools_to_foundry_tools(mcp_tools) -> list[dict]:
    """Convert MCP tool definitions to the OpenAI-style function schema that
    Foundry Local's chat client (and Azure OpenAI) both expect."""
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description or "",
                "parameters": tool.inputSchema,
            },
        }
        for tool in mcp_tools
    ]


def extract_text(call_tool_result) -> str:
    parts = [c.text for c in call_tool_result.content if hasattr(c, "text")]
    return "\n".join(parts) if parts else json.dumps(call_tool_result.model_dump())


async def run_tool_calling_turn(complete_chat, session: ClientSession, tools, prompt: str) -> dict:
    """Runs one prompt through the tool-calling loop and reports whether a
    tool was actually invoked, so reliability can be compared across models."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]

    tool_call_count = 0
    response = complete_chat(messages, tools=tools)
    choice = response.choices[0].message

    while choice.tool_calls:
        assistant_msg = {
            "role": "assistant",
            "content": choice.content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": tc.type,
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in choice.tool_calls
            ],
        }
        messages.append(assistant_msg)

        for tool_call in choice.tool_calls:
            tool_call_count += 1
            name = tool_call.function.name
            try:
                arguments = json.loads(tool_call.function.arguments)
            except json.JSONDecodeError:
                arguments = {}

            print(f"    -> tool call: {name}({arguments})")
            result = await session.call_tool(name, arguments=arguments)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": extract_text(result),
                }
            )

        response = complete_chat(messages, tools=tools)
        choice = response.choices[0].message

    return {"prompt": prompt, "answer": choice.content, "tool_calls": tool_call_count}


def get_foundry_manager() -> FoundryLocalManager:
    """Returns the process-wide FoundryLocalManager singleton, initializing it
    on first use. FoundryLocalManager.initialize() raises if called a second
    time, so a --model sweep must reuse the same manager across every model
    instead of re-initializing it per model."""
    if FoundryLocalManager.instance is None:
        FoundryLocalManager.initialize(Configuration(app_name=APP_NAME))
        FoundryLocalManager.instance.download_and_register_eps()
    return FoundryLocalManager.instance


def load_local_model(model_alias: str):
    manager = get_foundry_manager()
    model = manager.catalog.get_model(model_alias)
    if model is None:
        raise ValueError(f"Model alias '{model_alias}' not found in the Foundry Local catalog.")

    # See schema_hardening_eval.py's load_local_model() for why this is
    # printed: a model cataloged as not tool-capable produces the exact
    # same NO_TOOL_CALL result as one that's simply too weak to use the
    # tool, so this turns that ambiguity into a checkable fact.
    print(
        f"  capabilities={model.capabilities!r} "
        f"supports_tool_calling={model.supports_tool_calling!r}"
    )

    model.download(lambda progress: None)
    model.load()
    return model, model.get_chat_client()


# Tried in order when AZURE_OPENAI_DEPLOYMENT isn't set explicitly - prefer
# gpt-4.1 as the frontier-model comparison point, fall back to gpt-4o. Only
# works if your deployment is actually named after its model (the common
# case for portal-created deployments); set AZURE_OPENAI_DEPLOYMENT yourself
# if you've named yours something else.
DEFAULT_CLOUD_DEPLOYMENT_CANDIDATES = ["gpt-4.1", "gpt-4o"]


def load_cloud_client():
    import os

    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
    api_key = os.environ.get("AZURE_OPENAI_API_KEY")
    if not (endpoint and api_key):
        return None

    from openai import OpenAI

    # The unified /openai/v1/ surface takes a deployment name in `model`
    # like the classic AzureOpenAI client, but needs no api_version pin -
    # one less thing to keep updated as new models (like gpt-4.1) ship.
    client = OpenAI(api_key=api_key, base_url=f"{endpoint.rstrip('/')}/openai/v1/")

    explicit_deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT")
    candidates = [explicit_deployment] if explicit_deployment else DEFAULT_CLOUD_DEPLOYMENT_CANDIDATES

    deployment = None
    for candidate in candidates:
        try:
            client.chat.completions.create(
                model=candidate, messages=[{"role": "user", "content": "ping"}], max_tokens=1
            )
            deployment = candidate
            break
        except Exception as exc:  # noqa: BLE001 - just probing which deployment exists
            print(f"[Cloud] Deployment '{candidate}' unavailable ({exc}); trying next candidate.")

    if deployment is None:
        return None

    print(f"[Cloud] Using Azure OpenAI deployment '{deployment}'")

    def complete_chat(messages, tools):
        return client.chat.completions.create(model=deployment, messages=messages, tools=tools)

    return complete_chat


async def run_backend(session: ClientSession, tools: list[dict], label: str, complete_chat, report: dict) -> None:
    """Runs every eval prompt against one backend and records the outcomes -
    factored out so it can be called once per local model in a --model
    sweep, and again for the (single, fixed) cloud comparison."""
    print(f"=== {label} ===")
    report[label] = []
    for prompt in EVAL_PROMPTS:
        print(f"  [User]: {prompt}")
        outcome = await run_tool_calling_turn(complete_chat, session, tools, prompt)
        print(f"  [Assistant]: {outcome['answer']}\n")
        report[label].append(outcome)


async def main(compare_cloud: bool, model_aliases: list[str]) -> None:
    server_params = StdioServerParameters(command=sys.executable, args=[SERVER_SCRIPT])

    report: dict[str, list[dict]] = {}

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            mcp_tools = (await session.list_tools()).tools
            tools = mcp_tools_to_foundry_tools(mcp_tools)
            print(f"[MCP] Discovered {len(tools)} tool(s): {[t.name for t in mcp_tools]}\n")

            # One local model at a time, loaded then unloaded before the next -
            # a --model sweep across several sizes would otherwise try to hold
            # every model's weights in memory simultaneously.
            for model_alias in model_aliases:
                print(f"[Local] Loading Foundry Local model '{model_alias}'...")
                model, local_chat = load_local_model(model_alias)
                await run_backend(session, tools, f"local ({model_alias})", local_chat.complete_chat, report)
                model.unload()

            if compare_cloud:
                cloud_complete = load_cloud_client()
                if cloud_complete:
                    await run_backend(session, tools, "cloud (Azure OpenAI)", cloud_complete, report)
                else:
                    print(
                        "[Cloud] --compare-cloud set but AZURE_OPENAI_ENDPOINT / "
                        "AZURE_OPENAI_API_KEY / AZURE_OPENAI_DEPLOYMENT are missing - skipping.\n"
                    )

    print("=== Tool-call reliability summary ===")
    for label, outcomes in report.items():
        called = sum(1 for o in outcomes if o["tool_calls"] > 0)
        print(f"{label}: called a tool in {called}/{len(outcomes)} prompts that required one")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--compare-cloud",
        action="store_true",
        help="Also run the same evaluation against an Azure OpenAI deployment.",
    )
    parser.add_argument(
        "--model",
        nargs="+",
        default=[DEFAULT_MODEL_ALIAS],
        metavar="ALIAS",
        help=(
            "One or more Foundry Local model aliases to test, run one after "
            "another (default: %(default)s), e.g. "
            "`--model qwen2.5-1.5b qwen2.5-7b qwen3.5-0.8b qwen3.5-2b qwen3.5-4b`. "
            "Run `foundry model list` to see what's available on your hardware."
        ),
    )
    args = parser.parse_args()
    asyncio.run(main(args.compare_cloud, args.model))
