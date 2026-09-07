"""Connect a Foundry Local chat model to tools exposed over MCP, and run the
same tool-calling evaluation against a small local model and (optionally) a
large cloud frontier model to compare tool-call reliability.

This is the core question of Part 2: does a compact SLM running on-device
call tools as reliably as a large model in the cloud, given the exact same
tool schema? MCP is what lets both backends share that schema unchanged -
the server in mcp_server.py doesn't know or care which model is calling it.

Usage:
    python mcp_client.py                 # local model only
    python mcp_client.py --compare-cloud # local model + Azure OpenAI, side by side
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from foundry_local_sdk import Configuration, FoundryLocalManager

MODEL_ALIAS = "qwen2.5-0.5b"
APP_NAME = "from_edge_to_enterprise_mcp_agent"
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


def load_local_model():
    config = Configuration(app_name=APP_NAME)
    FoundryLocalManager.initialize(config)
    manager = FoundryLocalManager.instance
    manager.download_and_register_eps()

    model = manager.catalog.get_model(MODEL_ALIAS)
    model.download(lambda progress: None)
    model.load()
    return model, model.get_chat_client()


def load_cloud_client():
    import os

    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
    api_key = os.environ.get("AZURE_OPENAI_API_KEY")
    deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT")
    if not (endpoint and api_key and deployment):
        return None

    from openai import AzureOpenAI

    client = AzureOpenAI(azure_endpoint=endpoint, api_key=api_key, api_version="2024-10-21")

    def complete_chat(messages, tools):
        response = client.chat.completions.create(
            model=deployment, messages=messages, tools=tools
        )
        return response

    return complete_chat


async def main(compare_cloud: bool) -> None:
    server_params = StdioServerParameters(command=sys.executable, args=[SERVER_SCRIPT])

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            mcp_tools = (await session.list_tools()).tools
            tools = mcp_tools_to_foundry_tools(mcp_tools)
            print(f"[MCP] Discovered {len(tools)} tool(s): {[t.name for t in mcp_tools]}\n")

            print("[Local] Loading Foundry Local model...")
            model, local_chat = load_local_model()

            backends = {"local (qwen2.5-0.5b)": local_chat.complete_chat}
            if compare_cloud:
                cloud_complete = load_cloud_client()
                if cloud_complete:
                    backends["cloud (Azure OpenAI)"] = cloud_complete
                else:
                    print(
                        "[Cloud] --compare-cloud set but AZURE_OPENAI_ENDPOINT / "
                        "AZURE_OPENAI_API_KEY / AZURE_OPENAI_DEPLOYMENT are missing - skipping.\n"
                    )

            report: dict[str, list[dict]] = {}
            for label, complete_chat in backends.items():
                print(f"=== {label} ===")
                report[label] = []
                for prompt in EVAL_PROMPTS:
                    print(f"  [User]: {prompt}")
                    outcome = await run_tool_calling_turn(complete_chat, session, tools, prompt)
                    print(f"  [Assistant]: {outcome['answer']}\n")
                    report[label].append(outcome)

            model.unload()

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
    args = parser.parse_args()
    asyncio.run(main(args.compare_cloud))
