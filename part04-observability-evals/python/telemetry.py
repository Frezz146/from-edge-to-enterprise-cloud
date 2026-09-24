"""OpenTelemetry setup for eval_pipeline.py.

One switch, four destinations, so the same evaluation run can be traced on a
laptop at zero cost or in Azure next to the agent's own traces:

  none     no provider is configured, every span is a no-op (the default)
  console  spans are printed to stdout, handy for a first look
  otlp     spans go to any OTLP/HTTP endpoint, e.g. a local Aspire Dashboard
           (OTEL_EXPORTER_OTLP_ENDPOINT, default http://localhost:4318)
  azure    spans go to the Application Insights resource from infra/main.bicep,
           authenticated with Microsoft Entra ID instead of an ingestion key

The azure mode resolves the connection string in this order:
  1. APPLICATIONINSIGHTS_CONNECTION_STRING, if set
  2. the Application Insights connection registered on the Foundry project
     (FOUNDRY_PROJECT_ENDPOINT), which is exactly what infra/main.bicep creates

The connection string only tells the exporter where to send data. With
DisableLocalAuth on the Application Insights resource, ingestion is only
accepted with an Entra ID token, which is why DefaultAzureCredential is passed
in and why the identity needs the Monitoring Metrics Publisher role.

The spans this pipeline emits carry case IDs, outcomes and scores, never the
prompt or the model's arguments. Content recording is a separate decision; see
../README.md ("Data privacy and content recording").
"""

import os

from opentelemetry import trace

TRACE_MODES = ("none", "console", "otlp", "azure")


def _azure_connection_string() -> str:
    connection_string = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING")
    if connection_string:
        return connection_string

    project_endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT")
    if not project_endpoint:
        raise SystemExit(
            "--trace azure needs APPLICATIONINSIGHTS_CONNECTION_STRING or "
            "FOUNDRY_PROJECT_ENDPOINT (to read the project's Application Insights "
            "connection created by infra/main.bicep)."
        )

    from azure.ai.projects import AIProjectClient
    from azure.identity import DefaultAzureCredential

    client = AIProjectClient(endpoint=project_endpoint, credential=DefaultAzureCredential())
    return client.telemetry.get_application_insights_connection_string()


def configure_tracing(mode: str, service_name: str) -> None:
    """Installs a global tracer provider for `mode`. Call once at startup."""
    if mode == "none":
        return
    if mode not in TRACE_MODES:
        raise ValueError(f"Unknown trace mode '{mode}', expected one of {TRACE_MODES}")

    from opentelemetry.sdk.resources import Resource

    resource = Resource.create({"service.name": service_name})

    if mode == "azure":
        from azure.identity import DefaultAzureCredential
        from azure.monitor.opentelemetry import configure_azure_monitor

        configure_azure_monitor(
            connection_string=_azure_connection_string(),
            credential=DefaultAzureCredential(),
            resource=resource,
        )
        return

    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

    provider = TracerProvider(resource=resource)
    if mode == "console":
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    else:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        base = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{base.rstrip('/')}/v1/traces"))
        )
    trace.set_tracer_provider(provider)


def shutdown_tracing() -> None:
    """Flushes buffered spans before the process exits (a CI job ends right
    after the gate, so a batch processor would otherwise drop the tail)."""
    provider = trace.get_tracer_provider()
    shutdown = getattr(provider, "shutdown", None)
    if callable(shutdown):
        shutdown()
