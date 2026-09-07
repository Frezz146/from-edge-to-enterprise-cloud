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

  NO_TOOL_CALL     - the model answered instead of attempting a tool call,
                     in any format
  WRONG_TOOL       - it called a different tool than the one offered
  MALFORMED_JSON   - the tool-call arguments weren't valid JSON
  SERVER_REJECTED  - the MCP server's own schema validation rejected the
                     call (almost exclusively against the strict tool, plus
                     the one thing the loose tool still validates: duration
                     must parse as a number)
  WRONG_NAME / WRONG_EMAIL / WRONG_PRIORITY / WRONG_DURATION / MISSING_TOPIC
                   - the call succeeded, but a value doesn't match what
                     the prompt actually asked for - this is what the
                     loose schema lets slip through silently
  OK               - every field matches

Some models (observed: the qwen3.5 family, via this SDK version) don't
produce an OpenAI-style tool_calls entry at all - they write the call as
plain text in a custom <tool_call><function=...><parameter=...> tag format
instead. parse_fallback_tool_call() recovers that so the study measures the
model's actual argument accuracy rather than mislabeling every one of these
as NO_TOOL_CALL, which would conflate "the client didn't recognize this
call format" with "the model didn't try to call anything" - two very
different findings for the same category.

Built directly on the raw MCP and OpenAI-compatible chat completion APIs
rather than a higher-level agent framework: those auto-execute tool calls
and only hand back the final text, which would hide the very thing this
script measures - a tool call's raw arguments and outcome, inspected before
and around execution. This is the concrete, measurable version of "harden
your tool schemas for small models": a table of clean-call rates per
(backend, schema variant), plus an optional bar chart for blog use.

Usage:
    python schema_hardening_eval.py                          # local model only
    python schema_hardening_eval.py --compare-cloud           # + Azure OpenAI
    python schema_hardening_eval.py --model qwen2.5-1.5b      # a bigger model in the same family
    python schema_hardening_eval.py \
        --model qwen2.5-1.5b qwen2.5-7b qwen3.5-0.8b qwen3.5-2b qwen3.5-4b  # sweep several models

--model (repeatable, space-separated) lets you test whether the
SERVER_REJECTED-dominated failure mode documented in ../README.md ("A note
on flat vs. nested...") is specific to qwen2.5-0.5b or holds across model
sizes and families - each model is loaded, tested and unloaded in turn, so
you get one row per model in the summary table without holding every
model's weights in memory at once. Run `foundry model list` to see what's
actually available on your hardware before picking aliases.
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

DEFAULT_MODEL_ALIAS = "qwen2.5-0.5b"
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


# Ten prompts of increasing phrasing difficulty - explicit values, then
# multiple topics, then informal/indirect wording ("low-key", "nothing
# urgent", "standard-priority") a model has to translate into the exact
# expected enum value. A small n (like the original 3) only gives coarse
# 0/33/67/100% clean-rate buckets; ten cases gives 10% resolution, which
# matters for a table or chart meant to show a real gap, not just a hunch.
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
    Case(
        prompt=(
            "Book a high-priority one-hour meeting with Morgan Lee "
            "(morgan.lee@example.com) about the security audit."
        ),
        expected_name="Morgan Lee",
        expected_email="morgan.lee@example.com",
        expected_priority="high",
        expected_duration=60,
        expected_topics=["security audit"],
    ),
    Case(
        prompt=(
            "Arrange a medium-priority 45-minute session with Taylor Brooks "
            "(taylor.brooks@example.com) to go over hiring, budget planning, "
            "and vendor contracts."
        ),
        expected_name="Taylor Brooks",
        expected_email="taylor.brooks@example.com",
        expected_priority="medium",
        expected_duration=45,
        expected_topics=["hiring", "budget planning", "vendor contracts"],
    ),
    Case(
        prompt=(
            "This is urgent - set up a high-priority 20-minute call with Priya Patel "
            "(priya.patel@example.com) about the production outage."
        ),
        expected_name="Priya Patel",
        expected_email="priya.patel@example.com",
        expected_priority="high",
        expected_duration=20,
        expected_topics=["production outage"],
    ),
    Case(
        prompt=(
            "Just a quick ten-minute, no-rush chat with Devon Clarke "
            "(devon.clarke@example.com) about the intern onboarding checklist."
        ),
        expected_name="Devon Clarke",
        expected_email="devon.clarke@example.com",
        expected_priority="low",
        expected_duration=10,
        expected_topics=["intern onboarding checklist"],
    ),
    Case(
        prompt=(
            "Schedule a standard-priority 30-minute sync with Riley Nguyen "
            "(riley.nguyen@example.com) covering the Q4 planning doc."
        ),
        expected_name="Riley Nguyen",
        expected_email="riley.nguyen@example.com",
        expected_priority="medium",  # "standard" has no exact enum match - medium is the reasonable read
        expected_duration=30,
        expected_topics=["Q4 planning doc"],
    ),
    Case(
        prompt=(
            "Set up a low-priority 15-minute check-in with Casey Morgan "
            "(casey.morgan@example.com) about desk setup and badge access."
        ),
        expected_name="Casey Morgan",
        expected_email="casey.morgan@example.com",
        expected_priority="low",
        expected_duration=15,
        expected_topics=["desk setup", "badge access"],
    ),
    Case(
        prompt=(
            "This is critical - schedule a high-priority 60-minute review with "
            "Jordan Ellis (jordan.ellis@example.com) covering the compliance "
            "audit and the incident report."
        ),
        expected_name="Jordan Ellis",
        expected_email="jordan.ellis@example.com",
        expected_priority="high",
        expected_duration=60,
        expected_topics=["compliance audit", "incident report"],
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
        topics_text = (
            " ".join(topics) if isinstance(topics, list) else str(topics or "")
        )
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


# Some models (observed: the qwen3.5 family via this Foundry Local SDK
# version) don't emit an OpenAI-style tool_calls entry at all - instead they
# write the call as plain text in a custom tag format:
#   <tool_call><function=NAME><parameter=KEY>VALUE</parameter>...</function></tool_call>
# `choice.tool_calls` then comes back empty even though the model attempted
# (and, going by the raw text, largely got right) a real call - a client
# that only checks `tool_calls` would misclassify this as NO_TOOL_CALL,
# conflating "wrong client-side parsing" with "model didn't try". This
# recovers the call so the study measures the model's actual argument
# accuracy instead.
_FALLBACK_TOOL_CALL_RE = re.compile(
    r"<tool_call>\s*<function=(?P<name>[^>]+)>(?P<body>.*?)</function>\s*</tool_call>",
    re.DOTALL,
)
_FALLBACK_PARAM_RE = re.compile(
    r"<parameter=(?P<key>[^>]+)>(?P<value>.*?)</parameter>", re.DOTALL
)


def parse_fallback_tool_call(
    content: str, param_types: dict[str, str] | None = None
) -> tuple[str, dict] | None:
    """Best-effort parser for the <tool_call><function=...><parameter=...>
    tag format described above. Returns (function_name, arguments) or None
    if `content` doesn't match it.

    `param_types` should map each parameter name to its JSON Schema "type"
    (from the tool's own inputSchema, e.g. {"duration_minutes": "integer"}).
    A parameter typed "string" is kept as the literal text between its tags,
    since this tag format carries no quoting to distinguish the string "30"
    from the integer 30 the way real tool-call JSON would - trusting the
    schema instead of guessing avoids a real bug this had: json.loads-ing
    every value turned a schema-correct string like duration_minutes="30"
    into the int 30, which the MCP server's own Pydantic validation then
    rejected as SERVER_REJECTED even though the model got the call right.
    Every other type (object, array, integer, missing/unknown - e.g. a
    "$ref"'d nested object has no "type" key at all) is JSON-decoded when
    possible and kept as a raw string otherwise."""
    match = _FALLBACK_TOOL_CALL_RE.search(content)
    if not match:
        return None

    name = match.group("name").strip()
    param_types = param_types or {}
    args: dict = {}
    for param in _FALLBACK_PARAM_RE.finditer(match.group("body")):
        key = param.group("key").strip()
        raw_value = param.group("value").strip()
        if param_types.get(key) == "string":
            args[key] = raw_value
            continue
        try:
            args[key] = json.loads(raw_value)
        except json.JSONDecodeError:
            args[key] = raw_value
    return name, args


async def run_case(
    complete_chat, session: ClientSession, tool: dict, tool_name: str, case: Case
) -> list[str]:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": case.prompt},
    ]
    response = complete_chat(messages, tools=[tool])
    choice = response.choices[0].message

    if choice.tool_calls:
        call = choice.tool_calls[0]
        call_name = call.function.name
        try:
            args = json.loads(call.function.arguments)
        except json.JSONDecodeError:
            return ["MALFORMED_JSON"]
    else:
        properties = (
            tool.get("function", {}).get("parameters", {}).get("properties", {})
        )
        param_types = {key: prop.get("type") for key, prop in properties.items()}
        fallback = parse_fallback_tool_call(choice.content or "", param_types)
        if fallback is None:
            # Genuinely no attempt at a tool call, in any format - print what
            # the model said instead, since a NO_TOOL_CALL rate this high is
            # worth seeing the raw text for.
            preview = (choice.content or "").strip().replace("\n", " ")[:200]
            print(f"    (no tool call - model said: {preview!r})")
            return ["NO_TOOL_CALL"]

        call_name, args = fallback
        print(
            f"    (client didn't parse the tool call - recovered via fallback parser: {call_name}({args}))"
        )

    if call_name != tool_name:
        return ["WRONG_TOOL"]

    result = await session.call_tool(tool_name, arguments=args)
    if result.isError:
        # Print what was actually sent and the server's own validation
        # message - "SERVER_REJECTED" alone doesn't say *which* field or
        # type was wrong, and that's the difference between "this model
        # can't produce a nested object at all" and "it got everything
        # right except one enum casing".
        error_text = "; ".join(c.text for c in result.content if hasattr(c, "text"))
        print(f"    (rejected args: {args} | server said: {error_text})")
        return ["SERVER_REJECTED"]

    return classify_semantic(tool_name, args, case) or ["OK"]


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
        raise ValueError(
            f"Model alias '{model_alias}' not found in the Foundry Local catalog."
        )

    # The catalog manifest tags each model with capabilities (e.g.
    # "chat,completion") and an explicit supports_tool_calling flag. If a
    # model is cataloged as not tool-capable, the runtime won't get a real
    # chance to call anything - every case comes back NO_TOOL_CALL
    # regardless of schema, which looks identical to (but is not) the model
    # simply being too weak to use the tool. Printing this up front turns
    # that ambiguity into a checkable fact instead of a guess.
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
    candidates = (
        [explicit_deployment]
        if explicit_deployment
        else DEFAULT_CLOUD_DEPLOYMENT_CANDIDATES
    )

    deployment = None
    for candidate in candidates:
        try:
            client.chat.completions.create(
                model=candidate,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
            )
            deployment = candidate
            break
        except Exception as exc:  # noqa: BLE001 - just probing which deployment exists
            print(
                f"[Cloud] Deployment '{candidate}' unavailable ({exc}); trying next candidate."
            )

    if deployment is None:
        return None

    print(f"[Cloud] Using Azure OpenAI deployment '{deployment}'")

    def complete_chat(messages, tools):
        return client.chat.completions.create(
            model=deployment, messages=messages, tools=tools
        )

    return complete_chat


@dataclass
class VariantResult:
    """Tally for one (backend, schema variant) combination across all cases."""

    clean: int = 0
    error_counts: Counter = field(default_factory=Counter)


def print_summary(
    results: dict[tuple[str, str], VariantResult], total_cases: int
) -> None:
    print("\n=== Schema hardening summary ===")
    print(f"{'Backend':<22} | {'Schema':<6} | {'Clean calls':<12} | Errors")
    print("-" * 90)
    for (backend, variant), vr in results.items():
        error_items = [f"{code} x{n}" for code, n in vr.error_counts.items()]
        errors_str = ", ".join(error_items) if error_items else "-"
        print(
            f"{backend:<22} | {variant:<6} | {vr.clean}/{total_cases:<10} | {errors_str}"
        )


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

    # Widen the figure for a --model sweep with several backends - a fixed
    # 7" width gets cramped past two or three bars per group.
    fig, ax = plt.subplots(figsize=(max(7, 1.4 * len(backends)), 4))
    for i, variant in enumerate(variants):
        clean_rates = [
            100 * results[(backend, variant)].clean / total_cases
            for backend in backends
        ]
        offset = (i - 0.5) * width
        ax.bar([xi + offset for xi in x], clean_rates, width, label=variant)

    ax.set_xticks(list(x))
    ax.set_xticklabels(backends, rotation=20, ha="right")
    ax.set_ylabel("Clean call rate (%)")
    ax.set_ylim(0, 100)
    ax.set_title("Schema Hardening: Clean Tool-Call Rate by Schema Variant")
    ax.legend(title="Schema")
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)
    print(f"\n[Chart] Saved comparison chart to {output_path}")


async def run_backend(
    session: ClientSession,
    mcp_tools: dict,
    backend_label: str,
    complete_chat,
    results: dict[tuple[str, str], "VariantResult"],
) -> None:
    """Runs every (schema variant, case) combination against one backend and
    records the outcomes into `results`. Factored out so it can be called
    once per local model in a --model sweep, and again for the (single,
    fixed) cloud comparison, without duplicating the loop."""
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


async def main(compare_cloud: bool, model_aliases: list[str]) -> None:
    server_params = StdioServerParameters(command=sys.executable, args=[SERVER_SCRIPT])

    results: dict[tuple[str, str], VariantResult] = {}

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            mcp_tools = {t.name: t for t in (await session.list_tools()).tools}

            # One local model at a time, loaded then unloaded before the next -
            # a --model sweep across several sizes (e.g. the 0.5B/1.5B/7B Qwen2.5
            # family, or Qwen3.5's 0.8B/2B/4B tiers) would otherwise try to hold
            # every model's weights in memory simultaneously.
            for model_alias in model_aliases:
                print(f"\n[Local] Loading Foundry Local model '{model_alias}'...")
                model, local_chat = load_local_model(model_alias)
                await run_backend(
                    session,
                    mcp_tools,
                    f"local ({model_alias})",
                    local_chat.complete_chat,
                    results,
                )
                model.unload()

            if compare_cloud:
                cloud_complete = load_cloud_client()
                if cloud_complete:
                    await run_backend(
                        session,
                        mcp_tools,
                        "cloud (Azure OpenAI)",
                        cloud_complete,
                        results,
                    )
                else:
                    print(
                        "[Cloud] --compare-cloud set but AZURE_OPENAI_ENDPOINT / "
                        "AZURE_OPENAI_API_KEY / AZURE_OPENAI_DEPLOYMENT are missing - skipping.\n"
                    )

    print_summary(results, len(CASES))
    save_comparison_chart(results, len(CASES), CHART_PATH)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--compare-cloud",
        action="store_true",
        help="Also run the same study against an Azure OpenAI deployment.",
    )
    parser.add_argument(
        "--model",
        nargs="+",
        default=[DEFAULT_MODEL_ALIAS],
        metavar="ALIAS",
        help=(
            "One or more Foundry Local model aliases to test, space-separated "
            "(default: %(default)s). Run `foundry model list` to see what's "
            "available on your hardware. Each model is loaded, tested, and "
            "unloaded before the next, so this works as a sweep across a "
            "whole family's sizes - e.g. "
            "`--model qwen2.5-1.5b qwen2.5-7b qwen3.5-0.8b qwen3.5-2b qwen3.5-4b` "
            "to see whether the SERVER_REJECTED-heavy result documented in the "
            "README is specific to qwen2.5-0.5b or holds across model sizes "
            "and families."
        ),
    )
    args = parser.parse_args()
    asyncio.run(main(args.compare_cloud, args.model))
