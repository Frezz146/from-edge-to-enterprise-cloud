# Part 01: Local Development

Runs a small language model (`qwen2.5-0.5b`) entirely in-process on developer
hardware using the Foundry Local SDK's native chat completions API - no
container, no REST server, no cloud endpoint. This is the local loop the
rest of the series builds on: fast iteration on prompting and tool-calling
without spending cloud tokens on every run.

## What happens on first run

1. Foundry Local discovers your hardware and downloads the matching ONNX
   Runtime execution providers (NPU/GPU/CPU, whichever applies).
2. The `qwen2.5-0.5b` model variant optimized for your hardware is
   downloaded and cached.
3. The model is loaded into memory and served in-process - later runs skip
   steps 1-2 and load straight from cache.

## Run it

### Python

```bash
cd part01-local-development/python
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python local-loop.py
```

### C#

```bash
cd part01-local-development/csharp/local-loop
dotnet run
```

## Benchmark: local vs. cloud latency and throughput

### Python

```bash
cd part01-local-development/python
export AZURE_OPENAI_ENDPOINT="https://<resource>.openai.azure.com/"
export AZURE_OPENAI_API_KEY="<key>"
export AZURE_OPENAI_DEPLOYMENT="<deployment-name>"
python benchmark.py
```

### C#

```bash
cd part01-local-development/csharp/benchmark
export AZURE_OPENAI_ENDPOINT="https://<resource>.openai.azure.com/"
export AZURE_OPENAI_API_KEY="<key>"
export AZURE_OPENAI_DEPLOYMENT="<deployment-name>"
dotnet run
```

Both `benchmark.py` and the C# `benchmark` project run the same prompt
against the local model and, if the `AZURE_OPENAI_*` variables are set,
against an Azure OpenAI deployment, then print time-to-first-token, total
wall-clock time, and approximate tokens/sec for both. Without those
variables they still run, just local-only - the cloud comparison is
optional, not a hard dependency of this part. The C# benchmark lives in its
own project (`csharp/benchmark/`) rather than the main one, since a
top-level-statements program only has a single entry point - the same
reason Python keeps it as a second script instead of a second `main.py`.

`benchmark.py` additionally saves the comparison as a two-panel bar chart to
`latency-benchmark.png` next to the script (skipped with a hint if
`matplotlib` isn't installed - `pip install matplotlib`). The numbers depend
on your own hardware and Azure deployment, so regenerate it yourself rather
than reusing someone else's chart; the file isn't tracked in git for the
same reason. The C# benchmark does not generate a chart - use the Python
one for that, or plot `results` from either script's output yourself.

## What to look for

- **Time to first token** is where the local/cloud gap is usually largest -
  a cloud round-trip adds network latency a local process never pays.
- **Tokens/sec** tends to favor whichever backend has more compute behind
  it - a 0.5B model on a laptop NPU/GPU versus a much larger cloud model,
  so higher isn't automatically "better" here, it's a starting point for
  deciding what belongs on-device versus in the cloud.
- Re-run `main.py` a second time and notice there's no download step - the
  execution providers and model stay cached locally.
