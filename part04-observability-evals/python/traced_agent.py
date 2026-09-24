"""The Part 3 cloud agent, now with client-side OpenTelemetry tracing.

Agent Framework instruments every agent run, model call and function tool
invocation with the OpenTelemetry GenAI semantic conventions out of the box.
The only decision left is where the spans go:

  --target console   print spans to stdout
  --target otlp      send spans to a local Aspire Dashboard (inner loop, free)
  --target azure     send spans to the Application Insights resource that
                     infra/main.bicep connected to the Foundry project

For --target azure the connection string is not configured anywhere in this
script: FoundryChatClient.configure_azure_monitor() reads it from the
project's Application Insights connection. DefaultAzureCredential is passed
through because the Application Insights resource has DisableLocalAuth set,
so every export needs an Entra ID token (Monitoring Metrics Publisher).

Prompts and responses are NOT recorded by default. Pass --sensitive (or set
ENABLE_SENSITIVE_DATA=true) only in a dev or staging project where storing
message content in telemetry has been agreed on.

The tools and agent name are imported from Part 3 unchanged, so this traces
the exact agent Part 3 deployed.

Required environment variables (same as Part 3):
  FOUNDRY_PROJECT_ENDPOINT  e.g. https://<account>.services.ai.azure.com/api/projects/<project>
  FOUNDRY_MODEL             e.g. gpt-5-mini
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from agent_framework import Agent
from agent_framework.foundry import FoundryChatClient
from agent_framework.observability import configure_otel_providers, get_tracer
from azure.identity import DefaultAzureCredential
from opentelemetry import trace

PART03_PYTHON = Path(__file__).resolve().parents[2] / "part03-cloud-migration" / "python"
sys.path.insert(0, str(PART03_PYTHON))

from cloud_agent import AGENT_NAME, calculate, get_weather  # noqa: E402

QUESTIONS = [
    "What's the weather in Tokyo and what is 42 * 17?",
    "Convert that temperature to Fahrenheit, please.",
]


async def main(target: str, sensitive: bool) -> None:
    credential = DefaultAzureCredential()
    client = FoundryChatClient(credential=credential)

    if target == "azure":
        await client.configure_azure_monitor(enable_sensitive_data=sensitive, credential=credential)
    elif target == "otlp":
        configure_otel_providers(
            service_name="part04-traced-agent",
            otlp_endpoint=os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318"),
            otlp_protocol="http/protobuf",
            enable_sensitive_data=sensitive,
        )
    else:
        configure_otel_providers(
            service_name="part04-traced-agent",
            enable_console_exporters=True,
            enable_sensitive_data=sensitive,
        )

    agent = Agent(
        client=client,
        name=AGENT_NAME,
        instructions=(
            "You are a helpful assistant with access to tools. "
            "Use them when needed to answer questions accurately."
        ),
        tools=[get_weather, calculate],
    )

    # One parent span around the whole conversation, so both turns, every model
    # call and every tool invocation land under a single trace ID.
    with get_tracer().start_as_current_span("part04.traced_conversation") as span:
        session = agent.create_session()
        for question in QUESTIONS:
            print(f"[User]: {question}")
            result = await agent.run(question, session=session)
            print(f"[{AGENT_NAME}]: {result.text}")
        span.set_attribute("gen_ai.conversation.id", session.service_session_id or "")
        trace_id = format(span.get_span_context().trace_id, "032x")

    provider = trace.get_tracer_provider()
    if callable(getattr(provider, "force_flush", None)):
        provider.force_flush()

    print(f"\nTrace ID: {trace_id}")
    if target == "azure":
        print("Find it in Application Insights under Transaction search or query:")
        print(f"  dependencies | where operation_Id == '{trace_id}'")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", choices=("console", "otlp", "azure"), default="console")
    parser.add_argument(
        "--sensitive",
        action="store_true",
        help="Record prompts, responses and tool arguments in spans (dev and staging only).",
    )
    args = parser.parse_args()
    asyncio.run(main(args.target, args.sensitive))
