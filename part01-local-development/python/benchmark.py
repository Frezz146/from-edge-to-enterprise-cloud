"""Latency and throughput benchmark: local Foundry Local inference vs. a
cloud-hosted Azure OpenAI deployment, for the same prompt.

Measures:
  - time to first token (TTFT)
  - total wall-clock time
  - approximate tokens/sec (whitespace-split, not a real tokenizer count -
    good enough to compare relative throughput between the two backends)

The cloud comparison only runs when AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY
and AZURE_OPENAI_DEPLOYMENT are set, so this script also works completely
offline against the local model alone.
"""

import os
import time
from dataclasses import dataclass
from pathlib import Path

from foundry_local_sdk import Configuration, FoundryLocalManager

CHART_PATH = Path(__file__).parent / "latency-benchmark.png"

MODEL_ALIAS = "qwen2.5-0.5b"
PROMPT = "Explain in two sentences why hybrid edge/cloud AI architectures matter."


@dataclass
class RunResult:
    label: str
    time_to_first_token_s: float
    total_time_s: float
    approx_tokens: int

    @property
    def approx_tokens_per_sec(self) -> float:
        return self.approx_tokens / self.total_time_s if self.total_time_s else 0.0


def _stream_and_measure(label: str, chunks) -> RunResult:
    start = time.perf_counter()
    first_token_at = None
    approx_tokens = 0
    text_parts = []

    for content in chunks:
        if content is None:
            continue
        if first_token_at is None:
            first_token_at = time.perf_counter()
        text_parts.append(content)
        approx_tokens += max(1, len(content.split()))

    end = time.perf_counter()
    full_text = "".join(text_parts)
    print(f"  {full_text}\n")

    return RunResult(
        label=label,
        time_to_first_token_s=(first_token_at or end) - start,
        total_time_s=end - start,
        approx_tokens=approx_tokens,
    )


def run_local() -> RunResult:
    config = Configuration(app_name="frezz_tech_edge_agent")
    FoundryLocalManager.initialize(config)
    manager = FoundryLocalManager.instance
    manager.download_and_register_eps()

    model = manager.catalog.get_model(MODEL_ALIAS)
    model.download(lambda progress: None)
    model.load()
    client = model.get_chat_client()

    print("[Local | Foundry Local] streaming response:")

    def chunks():
        for chunk in client.complete_streaming_chat(
            [{"role": "user", "content": PROMPT}]
        ):
            if chunk.choices:
                yield chunk.choices[0].delta.content

    result = _stream_and_measure("local (Foundry Local)", chunks())
    model.unload()
    return result


def run_cloud() -> RunResult | None:
    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
    api_key = os.environ.get("AZURE_OPENAI_API_KEY")
    deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT")

    if not (endpoint and api_key and deployment):
        print(
            "[Cloud | Azure OpenAI] skipped - set AZURE_OPENAI_ENDPOINT, "
            "AZURE_OPENAI_API_KEY and AZURE_OPENAI_DEPLOYMENT to include it.\n"
        )
        return None

    from openai import AzureOpenAI

    client = AzureOpenAI(
        azure_endpoint=endpoint,
        api_key=api_key,
        api_version="2024-10-21",
    )

    print("[Cloud | Azure OpenAI] streaming response:")

    def chunks():
        stream = client.chat.completions.create(
            model=deployment,
            messages=[{"role": "user", "content": PROMPT}],
            stream=True,
        )
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    return _stream_and_measure("cloud (Azure OpenAI)", chunks())


def print_comparison(results: list[RunResult]) -> None:
    print("Backend               | TTFT (s) | Total (s) | ~tokens | ~tokens/s")
    print("-----------------------------------------------------------------")
    for r in results:
        print(
            f"{r.label:<22} | {r.time_to_first_token_s:8.3f} | "
            f"{r.total_time_s:9.3f} | {r.approx_tokens:7d} | "
            f"{r.approx_tokens_per_sec:9.1f}"
        )


def save_comparison_chart(results: list[RunResult], output_path: Path) -> None:
    """Renders TTFT and tokens/sec as a two-panel bar chart - the
    latency-vs-throughput illustration referenced from the blog post.

    Skips gracefully (with a hint) if matplotlib isn't installed, since it's
    an optional, chart-only dependency - not needed to run the benchmark itself.
    """
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        print(
            f"\n[Chart] matplotlib not installed - skipping {output_path.name}. "
            "Install it with `pip install matplotlib` to generate the chart."
        )
        return

    labels = [r.label for r in results]
    colors = ["#2E7D32", "#1565C0", "#EF6C00"][: len(labels)]
    ttft_ms = [r.time_to_first_token_s * 1000 for r in results]
    tokens_per_sec = [r.approx_tokens_per_sec for r in results]

    fig, (ttft_ax, throughput_ax) = plt.subplots(1, 2, figsize=(10, 4))

    ttft_ax.bar(labels, ttft_ms, color=colors)
    ttft_ax.set_title("Time to First Token")
    ttft_ax.set_ylabel("ms (lower is better)")
    ttft_ax.tick_params(axis="x", rotation=15)

    throughput_ax.bar(labels, tokens_per_sec, color=colors)
    throughput_ax.set_title("Throughput")
    throughput_ax.set_ylabel("~tokens/sec (higher is better)")
    throughput_ax.tick_params(axis="x", rotation=15)

    fig.suptitle("Foundry Local Benchmarking: Latency vs. Throughput")
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)
    print(f"\n[Chart] Saved comparison chart to {output_path}")


if __name__ == "__main__":
    results = [run_local()]
    cloud_result = run_cloud()
    if cloud_result:
        results.append(cloud_result)

    print("\n=== Latency & throughput comparison ===")
    print_comparison(results)
    save_comparison_chart(results, CHART_PATH)
