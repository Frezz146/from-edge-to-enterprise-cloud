"""Part 4 quality gate: run the golden dataset against a model and fail the
build when tool-calling quality regresses.

For every case in ../golden_dataset.json this script:
  1. Sends the query and the book_appointment_strict tool schema to the model
     under test: a local Foundry Local model (--backend local) or the cloud
     deployment from Part 3 through the Azure OpenAI v1 endpoint
     (--backend cloud).
  2. Executes the returned tool call against Part 2's own MCP server, so
     SERVER_REJECTED means exactly what it meant in Part 2: the server's
     Pydantic validation refused the call before the handler ran.
  3. Classifies the outcome with Part 2's error taxonomy. This step is
     deterministic, needs no model and costs nothing.
  4. Optionally (--judge) scores the tool call with the ToolCallAccuracyEvaluator
     from azure-ai-evaluation, an LLM judge on a 1 to 5 scale.

The gate thresholds live in the dataset (quality_gate):
  min_clean_call_rate     share of cases classified OK
  min_tool_call_accuracy  mean judge score, only checked with --judge

The script exits 1 when a threshold is missed, so a CI job can block the merge.

Nothing from Part 2 is copied here. The MCP server, the taxonomy classifier,
the fallback parser for tag-style tool calls and the local model loader are
imported from ../../part02-mcp-orchestration/python, so the gate and the study
can never drift apart.

Usage:
    python eval_pipeline.py                                  # local, qwen2.5-1.5b
    python eval_pipeline.py --model qwen2.5-7b               # the Part 2 regression
    python eval_pipeline.py --backend cloud --judge          # gpt-5-mini + LLM judge
    python eval_pipeline.py --trace otlp                     # spans to an Aspire Dashboard

Environment variables:
  FOUNDRY_LOCAL_MODEL     default local model alias (overridden by --model)
  AZURE_OPENAI_ENDPOINT   https://<account>.openai.azure.com, needed for
                          --backend cloud and --judge (Bicep output openAiEndpoint)
  FOUNDRY_MODEL           cloud deployment under test (default gpt-5-mini)
  JUDGE_MODEL             deployment used as judge (default gpt-5-mini)
  EVAL_TRACE              default for --trace (none, console, otlp, azure)

Authentication is Microsoft Entra ID only (DefaultAzureCredential): az login
locally, the OIDC federated credential in GitHub Actions. There is no API key.
"""

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from opentelemetry import trace

from telemetry import TRACE_MODES, configure_tracing, shutdown_tracing

PART04_DIR = Path(__file__).resolve().parents[1]
PART02_PYTHON = PART04_DIR.parent / "part02-mcp-orchestration" / "python"
sys.path.insert(0, str(PART02_PYTHON))

# Imported, not copied: this is the code that produced the Part 2 numbers.
from schema_hardening_eval import (  # noqa: E402
    SYSTEM_PROMPT,
    Case,
    classify_semantic,
    load_local_model,
    mcp_tool_to_foundry_tool,
    parse_fallback_tool_call,
)

DATASET_PATH = PART04_DIR / "golden_dataset.json"
MCP_SERVER_SCRIPT = PART02_PYTHON / "mcp_server.py"
RESULTS_PATH = Path(__file__).parent / "evaluation_results.json"

DEFAULT_LOCAL_MODEL = "qwen2.5-1.5b"
DEFAULT_CLOUD_MODEL = "gpt-5-mini"
AZURE_AI_SCOPE = "https://ai.azure.com/.default"
JUDGE_API_VERSION = "2025-04-01-preview"

tracer = trace.get_tracer("part04.eval_pipeline")


@dataclass
class CaseResult:
    id: str
    outcome: list[str]
    tool_call: dict | None = None
    server_message: str | None = None
    judge_score: float | None = None
    judge_reason: str | None = None

    @property
    def clean(self) -> bool:
        return self.outcome == ["OK"]


def require_env(name: str) -> str:
    """Fails fast with an actionable message instead of letting an empty value
    reach the Azure SDK. GitHub Actions turns a referenced but unconfigured
    variable into an empty string, not an unset one, so os.environ[...] alone
    would not raise and the SDK would fail later with a far less helpful error."""
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(
            f"{name} is not set. Locally: export {name}=... "
            f"In CI: add it under Settings -> Secrets and variables -> Actions -> Variables."
        )
    return value


def to_part2_case(case: dict) -> Case:
    """Maps a golden dataset entry onto Part 2's Case, so Part 2's own
    classify_semantic() judges it."""
    expected = case["expected_parameters"]
    return Case(
        prompt=case["query"],
        expected_name=expected["attendee"]["name"],
        expected_email=expected["attendee"]["email"],
        expected_priority=expected["priority"],
        expected_duration=expected["duration_minutes"],
        expected_topics=expected["topics"],
    )


# --- Backends -----------------------------------------------------------------


def open_local_backend(model_alias: str):
    """Foundry Local, same lifecycle as Parts 1 and 2."""
    print(f"[Local] Loading Foundry Local model '{model_alias}'...")
    model, chat_client = load_local_model(model_alias)
    # A gate has to be repeatable: the same commit should get the same verdict.
    chat_client.settings.temperature = 0.0
    return f"local ({model_alias})", chat_client.complete_chat, model.unload


def open_cloud_backend():
    """The Part 3 model deployment, called through the Azure OpenAI v1 endpoint
    with an Entra ID token (Cognitive Services OpenAI User, see infra/main.bicep)."""
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider
    from openai import OpenAI

    endpoint = require_env("AZURE_OPENAI_ENDPOINT")
    deployment = os.environ.get("FOUNDRY_MODEL", DEFAULT_CLOUD_MODEL)
    client = OpenAI(
        base_url=f"{endpoint.rstrip('/')}/openai/v1/",
        api_key=get_bearer_token_provider(DefaultAzureCredential(), AZURE_AI_SCOPE),
    )

    def complete_chat(messages, tools):
        return client.chat.completions.create(model=deployment, messages=messages, tools=tools)

    print(f"[Cloud] Using deployment '{deployment}' at {endpoint}")
    return f"cloud ({deployment})", complete_chat, lambda: None


def build_judge(threshold: float):
    """ToolCallAccuracyEvaluator with an Entra ID credential instead of api_key.

    Three details that are easy to get wrong with azure-ai-evaluation 1.18:
      - The credential goes to the evaluator's `credential` argument. Putting it
        into model_config["credential"], as the TypedDict suggests, fails the
        SDK's own config validation ("Model config validation failed").
      - The evaluators default to api_version 2024-02-15-preview, which predates
        reasoning models, so a current version is set explicitly.
      - gpt-5 family and o-series deployments reject the temperature and
        max_tokens settings in the evaluator's prompt template unless
        is_reasoning_model is set."""
    from azure.ai.evaluation import ToolCallAccuracyEvaluator
    from azure.identity import DefaultAzureCredential

    deployment = os.environ.get("JUDGE_MODEL", DEFAULT_CLOUD_MODEL)
    model_config = {
        "azure_endpoint": require_env("AZURE_OPENAI_ENDPOINT"),
        "azure_deployment": deployment,
        "api_version": JUDGE_API_VERSION,
    }
    print(f"[Judge] ToolCallAccuracyEvaluator on deployment '{deployment}'")
    return ToolCallAccuracyEvaluator(
        model_config=model_config,
        credential=DefaultAzureCredential(),
        threshold=threshold,
        is_reasoning_model=deployment.startswith(("gpt-5", "o1", "o3", "o4")),
    )


# --- One case -----------------------------------------------------------------


async def run_case(complete_chat, session: ClientSession, tool: dict, case: dict) -> CaseResult:
    """Mirrors run_case() in Part 2's schema_hardening_eval.py, but keeps the
    raw tool call so the judge can score it afterwards."""
    tool_name = tool["function"]["name"]
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": case["query"]},
    ]
    message = complete_chat(messages, tools=[tool]).choices[0].message

    if message.tool_calls:
        call = message.tool_calls[0]
        call_name = call.function.name
        try:
            args = json.loads(call.function.arguments)
        except json.JSONDecodeError:
            return CaseResult(case["id"], ["MALFORMED_JSON"])
    else:
        properties = tool["function"]["parameters"].get("properties", {})
        param_types = {key: prop.get("type") for key, prop in properties.items()}
        fallback = parse_fallback_tool_call(message.content or "", param_types)
        if fallback is None:
            return CaseResult(case["id"], ["NO_TOOL_CALL"])
        call_name, args = fallback

    tool_call = {"name": call_name, "arguments": args}
    if call_name != tool_name:
        return CaseResult(case["id"], ["WRONG_TOOL"], tool_call)

    result = await session.call_tool(tool_name, arguments=args)
    if result.isError:
        server_message = " ".join(
            " ".join(c.text.split()) for c in result.content if hasattr(c, "text")
        )
        return CaseResult(case["id"], ["SERVER_REJECTED"], tool_call, server_message)

    errors = classify_semantic(tool_name, args, to_part2_case(case))
    return CaseResult(case["id"], errors or ["OK"], tool_call)


def judge_case(judge, tool: dict, case: dict, result: CaseResult) -> None:
    """Scores one tool call. A case without a parseable call gets the rubric's
    floor (1.0), so a model that stops calling tools cannot raise the mean."""
    if result.tool_call is None:
        result.judge_score, result.judge_reason = 1.0, "No parseable tool call."
        return

    function = tool["function"]
    verdict = judge(
        query=case["query"],
        tool_calls=[
            {
                "type": "tool_call",
                "tool_call_id": f"call_{case['id']}",
                "name": result.tool_call["name"],
                "arguments": result.tool_call["arguments"],
            }
        ],
        tool_definitions=[
            {
                "name": function["name"],
                "description": function["description"],
                "parameters": function["parameters"],
            }
        ],
    )
    score = verdict.get("tool_call_accuracy")
    result.judge_score = float(score) if isinstance(score, (int, float)) else None
    result.judge_reason = verdict.get("tool_call_accuracy_reason")


# --- Reporting ----------------------------------------------------------------


def print_summary(backend: str, results: list[CaseResult], mean_score: float | None) -> None:
    clean = sum(r.clean for r in results)
    errors = Counter(code for r in results if not r.clean for code in r.outcome)
    error_text = ", ".join(f"{code} x{n}" for code, n in errors.items()) or "-"
    judge_text = f"{mean_score:.2f}/5" if mean_score is not None else "-"

    print("\n=== Quality gate summary ===")
    print(f"{'Backend':<30} | {'Clean calls':<11} | {'Judge':<7} | Errors")
    print("-" * 90)
    print(f"{backend:<30} | {clean}/{len(results):<9} | {judge_text:<7} | {error_text}")


def write_github_summary(backend: str, results: list[CaseResult], verdicts: list[str], passed: bool) -> None:
    """Adds a table to the workflow run page, so a reviewer sees which case
    failed without opening the log."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = [
        f"### Quality gate: {backend} {'passed' if passed else 'FAILED'}",
        "",
        *[f"- {v}" for v in verdicts],
        "",
        "| Case | Outcome | Judge |",
        "| --- | --- | --- |",
    ]
    for r in results:
        score = f"{r.judge_score:.1f}" if r.judge_score is not None else ""
        lines.append(f"| {r.id} | {'+'.join(r.outcome)} | {score} |")
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# --- Main ---------------------------------------------------------------------


async def evaluate(args: argparse.Namespace) -> int:
    dataset = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    gate = dataset["quality_gate"]
    cases = dataset["cases"]
    judge = build_judge(gate["min_tool_call_accuracy"]) if args.judge else None

    server = StdioServerParameters(command=sys.executable, args=[str(MCP_SERVER_SCRIPT)])
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            mcp_tools = {t.name: t for t in (await session.list_tools()).tools}
            tool = mcp_tool_to_foundry_tool(mcp_tools[dataset["tool"]])

            if args.backend == "local":
                backend, complete_chat, close = open_local_backend(args.model)
            else:
                backend, complete_chat, close = open_cloud_backend()

            results: list[CaseResult] = []
            with tracer.start_as_current_span("part04.eval_run") as run_span:
                run_span.set_attribute("eval.backend", backend)
                run_span.set_attribute("eval.dataset_version", dataset["version"])
                run_span.set_attribute("eval.case_count", len(cases))

                print(f"\n=== {backend} | {len(cases)} golden cases | tool {dataset['tool']} ===")
                for case in cases:
                    with tracer.start_as_current_span("part04.eval_case") as span:
                        result = await run_case(complete_chat, session, tool, case)
                        if judge is not None:
                            judge_case(judge, tool, case, result)

                        span.set_attribute("eval.case_id", case["id"])
                        span.set_attribute("eval.outcome", "+".join(result.outcome))
                        if result.judge_score is not None:
                            span.set_attribute("eval.tool_call_accuracy", result.judge_score)

                    results.append(result)
                    score = f" | judge {result.judge_score:.1f}" if result.judge_score is not None else ""
                    print(f"  [{case['id']}] {'+'.join(result.outcome)}{score}")
                    if result.server_message:
                        print(f"    server said: {result.server_message}")

                close()

                clean_rate = sum(r.clean for r in results) / len(results)
                scores = [r.judge_score for r in results if r.judge_score is not None]
                mean_score = sum(scores) / len(scores) if scores else None
                run_span.set_attribute("eval.clean_call_rate", clean_rate)
                if mean_score is not None:
                    run_span.set_attribute("eval.tool_call_accuracy_mean", mean_score)
                trace_id = format(run_span.get_span_context().trace_id, "032x")

    verdicts = [
        f"clean call rate {clean_rate:.0%} (min {gate['min_clean_call_rate']:.0%})",
    ]
    passed = clean_rate >= gate["min_clean_call_rate"]
    if judge is not None:
        verdicts.append(
            f"mean tool call accuracy {mean_score:.2f} (min {gate['min_tool_call_accuracy']})"
            if mean_score is not None
            else "mean tool call accuracy unavailable"
        )
        passed = passed and mean_score is not None and mean_score >= gate["min_tool_call_accuracy"]

    print_summary(backend, results, mean_score)
    for verdict in verdicts:
        print(f"  {verdict}")
    print(f"\nQuality gate {'PASSED' if passed else 'FAILED'}.")
    if args.trace != "none":
        print(f"Trace ID: {trace_id}")

    RESULTS_PATH.write_text(
        json.dumps(
            {
                "backend": backend,
                "dataset_version": dataset["version"],
                "passed": passed,
                "clean_call_rate": clean_rate,
                "tool_call_accuracy_mean": mean_score,
                "cases": [asdict(r) for r in results],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Results written to {RESULTS_PATH.name}")
    write_github_summary(backend, results, verdicts, passed)
    return 0 if passed else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--backend", choices=("local", "cloud"), default="local")
    parser.add_argument(
        "--model",
        default=os.environ.get("FOUNDRY_LOCAL_MODEL", DEFAULT_LOCAL_MODEL),
        help="Foundry Local model alias for --backend local (default: %(default)s).",
    )
    parser.add_argument("--judge", action="store_true", help="Also score every call with ToolCallAccuracyEvaluator.")
    parser.add_argument("--trace", choices=TRACE_MODES, default=os.environ.get("EVAL_TRACE", "none"))
    parser.add_argument("--dataset", default=str(DATASET_PATH))
    args = parser.parse_args()

    configure_tracing(args.trace, service_name="part04-eval-pipeline")
    try:
        return asyncio.run(evaluate(args))
    finally:
        shutdown_tracing()


if __name__ == "__main__":
    sys.exit(main())
