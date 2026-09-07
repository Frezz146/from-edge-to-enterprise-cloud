"""Schema hardening study: does tightening a tool's JSON schema actually make
a small local model call it more reliably?

mcp_server.py exposes the same conceptual task - "book a meeting" - through
two tools with different schema rigor:
  - book_appointment_loose: five flat, untyped string fields, vague
    one-line descriptions. Nothing stops the model from writing "urgent"
    instead of "high", or "30 minutes" instead of 30.
  - book_appointment_strict: a nested attendee object, an array of topics,
    a priority enum and an integer duration. The MCP server (via Pydantic)
    rejects a call that doesn't match this shape before it ever runs.

For each of three prompts (of increasing phrasing difficulty), this script
calls both tool variants through the same local model and, optionally, a
cloud model, and classifies every attempt into an error category:

  NO_TOOL_CALL     - the model answered instead of calling the tool
  WRONG_TOOL       - it called a different tool than the one offered
  MALFORMED_JSON   - the tool-call arguments weren't valid JSON
  SERVER_REJECTED  - the MCP server's own schema validation rejected the
                     call (only possible against the strict tool - the loose
                     one has no types to violate)
  WRONG_NAME / WRONG_EMAIL / WRONG_PRIORITY / WRONG_DURATION / MISSING_TOPIC
                   - the call succeeded, but a value doesn't match what
                     the prompt actually asked for - this is what the
                     loose schema lets slip through silently
  OK               - every field matches

Built directly on the raw MCP and OpenAI-compatible chat completion APIs
rather than a higher-level agent framework: those auto-execute tool calls
and only hand back the final text, which would hide the very thing this
script measures - a tool call's raw arguments and outcome, inspected before
and around execution. This is the concrete, measurable version of "harden
your tool schemas for small models": a table of clean-call rates per
(backend, schema variant), plus an optional bar chart for blog use.

Usage:
    python schema_hardening_eval.py                 # local model only
    python schema_hardening_eval.py --compare-cloud  # + Azure OpenAI
"""

import argparse
import asyncio
import json
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from foundry_local_sdk import Configuration, FoundryLocalManager

MODEL_ALIAS = "qwen2.5-0.5b"
APP_NAME = "frezz_tech_mcp_agent"
SERVER_SCRIPT = str(Path(__file__).parent / "mcp_server.py")
CHART_PATH = Path(__file__).parent / "schema-hardening.png"

SYSTEM_PROMPT = (
    "You are a scheduling assistant. Use the booking tool to schedule the "
    "requested meeting. Always call the tool - do not just describe what "
    "you would do."
)

SCHEMA_VARIANTS = {
    "loose": "book_appointment_loose",
    "strict": "book_appointment_strict",
}


@dataclass
class Case:
    prompt: str
    expected_name: str
    expected_email: str
    expected_priority: str  # one of "low", "medium", "high"
    expected_duration: int
    expected_topics: list[str]


# Three prompts of increasing phrasing difficulty - explicit values, then
# multiple topics, then informal/indirect wording ("low-key", "nothing
# urgent") a model has to translate into the exact expected enum value.
CASES = [
    Case(
        prompt=(
            "Schedule a high-priority 30-minute meeting with Jamie Chen "
            "(jamie.chen@example.com) to discuss the Q3 roadmap."
        ),
        expected_name="Jamie Chen",
        expected_email="jamie.chen@example.com",
        expected_priority="high",
        expected_duration=30,
        expected_topics=["Q3 roadmap"],
    ),
    Case(
        prompt=(
            "Set up a medium-priority, half-hour call with Alex Kim "
            "(alex.kim@example.com) to cover the MCP rollout and the Q3 roadmap."
        ),
        expected_name="Alex Kim",
        expected_email="alex.kim@example.com",
        expected_priority="medium",
        expected_duration=30,
        expected_topics=["MCP rollout", "Q3 roadmap"],
    ),
    Case(
        prompt=(
            "Set up a quick 15-minute, low-key chat with Sam Rivera "
            "(sam.rivera@example.com) about onboarding feedback - nothing urgent."
        ),
        expected_name="Sam Rivera",
        expected_email="sam.rivera@example.com",
        expected_priority="low",
        expected_duration=15,
        expected_topics=["onboarding feedback"],
    ),
]


def mcp_tool_to_foundry_tool(mcp_tool) -> dict:
    return {
        "type": "function",
        "function": {
            "name": mcp_tool.name,
            "description": mcp_tool.description or "",
            "parameters": mcp_tool.inputSchema,
        },
    }


def _extract_int(value) -> int | None:
    """Pulls the first integer out of a value that might be "30", "30
    minutes", or already an int - the loose schema allows all three."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        match = re.search(r"\d+", value)
        if match:
            return int(match.group())
    return None


def classify_semantic(tool_name: str, args: dict, case: Case) -> list[str]:
    """Checks the extracted values against what the prompt actually asked
    for. The strict schema already rejects wrong *types* at the server -
    this catches wrong *values* that both schemas would let through, so
    the two variants are compared on equal footing."""
    if tool_name == "book_appointment_strict":
        attendee = args.get("attendee") or {}
        name, email = attendee.get("name", ""), attendee.get("email", "")
        priority = args.get("priority", "")
        duration = args.get("duration_minutes")
        topics = args.get("topics")
        topics_text = " ".join(topics) if isinstance(topics, list) else str(topics or "")
    else:
        name = args.get("attendee_name", "")
        email = args.get("attendee_email", "")
        priority = args.get("priority", "")
        duration = args.get("duration_minutes")
        topics_text = str(args.get("topics", ""))

    errors = []
    if case.expected_name.lower() not in str(name).lower():
        errors.append("WRONG_NAME")
    if str(email).strip().lower() != case.expected_email.lower():
        errors.append("WRONG_EMAIL")
    if str(priority).strip().lower() != case.expected_priority:
        errors.append("WRONG_PRIORITY")
    if _extract_int(duration) != case.expected_duration:
        errors.append("WRONG_DURATION")
    if not all(topic.lower() in topics_text.lower() for topic in case.expected_topics):
        errors.append("MISSING_TOPIC")
    return errors


async def run_case(complete_chat, session: ClientSession, tool: dict, tool_name: str, case: Case) -> list[str]:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": case.prompt},
    ]
    response = complete_chat(messages, tools=[tool])
    choice = response.choices[0].message

    if not choice.tool_calls:
        return ["NO_TOOL_CALL"]

    call = choice.tool_calls[0]
    if call.function.name != tool_name:
        return ["WRONG_TOOL"]

    try:
        args = json.loads(call.function.arguments)
    except json.JSONDecodeError:
        return ["MALFORMED_JSON"]

    result = await session.call_tool(tool_name, arguments=args)
    if result.isError:
        return ["SERVER_REJECTED"]

    return classify_semantic(tool_name, args, case) or ["OK"]


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
    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
    api_key = os.environ.get("AZURE_OPENAI_API_KEY")
    deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT")
    if not (endpoint and api_key and deployment):
        return None

    from openai import AzureOpenAI

    client = AzureOpenAI(azure_endpoint=endpoint, api_key=api_key, api_version="2024-10-21")

    def complete_chat(messages, tools):
        return client.chat.completions.create(model=deployment, messages=messages, tools=tools)

    return complete_chat


@dataclass
class VariantResult:
    """Tally for one (backend, schema variant) combination across all cases."""

    clean: int = 0
    error_counts: Counter = field(default_factory=Counter)


def print_summary(results: dict[tuple[str, str], VariantResult], total_cases: int) -> None:
    print("\n=== Schema hardening summary ===")
    print(f"{'Backend':<22} | {'Schema':<6} | {'Clean calls':<12} | Errors")
    print("-" * 90)
    for (backend, variant), vr in results.items():
        error_items = [f"{code} x{n}" for code, n in vr.error_counts.items()]
        errors_str = ", ".join(error_items) if error_items else "-"
        print(f"{backend:<22} | {variant:<6} | {vr.clean}/{total_cases:<10} | {errors_str}")


def save_comparison_chart(
    results: dict[tuple[str, str], VariantResult], total_cases: int, output_path: Path
) -> None:
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        print(
            f"\n[Chart] matplotlib not installed - skipping {output_path.name}. "
            "Install it with `pip install matplotlib` to generate the chart."
        )
        return

    backends = list(dict.fromkeys(backend for backend, _ in results))
    variants = list(SCHEMA_VARIANTS.keys())
    x = range(len(backends))
    width = 0.35

    fig, ax = plt.subplots(figsize=(7, 4))
    for i, variant in enumerate(variants):
        clean_rates = [100 * results[(backend, variant)].clean / total_cases for backend in backends]
        offset = (i - 0.5) * width
        ax.bar([xi + offset for xi in x], clean_rates, width, label=variant)

    ax.set_xticks(list(x))
    ax.set_xticklabels(backends, rotation=10)
    ax.set_ylabel("Clean call rate (%)")
    ax.set_ylim(0, 100)
    ax.set_title("Schema Hardening: Clean Tool-Call Rate by Schema Variant")
    ax.legend(title="Schema")
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)
    print(f"\n[Chart] Saved comparison chart to {output_path}")


async def main(compare_cloud: bool) -> None:
    server_params = StdioServerParameters(command=sys.executable, args=[SERVER_SCRIPT])

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            mcp_tools = {t.name: t for t in (await session.list_tools()).tools}

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

            results: dict[tuple[str, str], VariantResult] = {}
            for backend_label, complete_chat in backends.items():
                for variant_label, tool_name in SCHEMA_VARIANTS.items():
                    tool = mcp_tool_to_foundry_tool(mcp_tools[tool_name])
                    vr = VariantResult()
                    print(f"\n=== {backend_label} | {variant_label} schema ({tool_name}) ===")
                    for case in CASES:
                        errors = await run_case(complete_chat, session, tool, tool_name, case)
                        print(f"  [{case.expected_name}] {'+'.join(errors)}")
                        if errors == ["OK"]:
                            vr.clean += 1
                        else:
                            vr.error_counts.update(errors)
                    results[(backend_label, variant_label)] = vr

            model.unload()

    print_summary(results, len(CASES))
    save_comparison_chart(results, len(CASES), CHART_PATH)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--compare-cloud",
        action="store_true",
        help="Also run the same study against an Azure OpenAI deployment.",
    )
    args = parser.parse_args()
    asyncio.run(main(args.compare_cloud))
