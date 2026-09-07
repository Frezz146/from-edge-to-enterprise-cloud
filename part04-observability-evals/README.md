# Part 04: Observability and Evaluations

Automated quality gate for the agent built in Parts 1-3: a golden dataset of
(query, context) pairs, a script that regenerates each answer with the local
Foundry Local model and scores its groundedness with an LLM judge, and a
threshold that fails the build when quality regresses.

## What this catches

`eval_pipeline.py` answers the question at the heart of this part: **did the
last model update or prompt change make the agent worse?** A model swap or a
tweaked system prompt can silently increase hallucination even when nothing
crashes - this pipeline turns that into a measurable, CI-blocking number
instead of something only noticed after users complain.

## Run it locally

```bash
cd part04-observability-evals/python
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt

export AZURE_OPENAI_ENDPOINT="https://<resource>.openai.azure.com/"
export AZURE_OPENAI_API_KEY="<key>"
export AZURE_OPENAI_DEPLOYMENT="<judge-model-deployment-name>"

python eval_pipeline.py
```

The script downloads/loads `qwen2.5-0.5b` locally (same as Part 1), answers
every case in `golden_dataset.json` grounded in its `context`, then sends the
(query, context, response) triples to `GroundednessEvaluator` from
`azure-ai-evaluation`. It exits `0` when the mean groundedness score is at or
above `groundedness_threshold` in the dataset, and `1` otherwise.

## Extending the golden dataset

Add cases to `golden_dataset.json` as `{id, query, context}` - no code changes
needed. Cases mirroring realistic tool-calling scenarios from Part 2 are the
most useful regression coverage, since that's where small models are most
likely to drift.

## CI/CD

`.github/workflows/eval.yml` runs this pipeline on every push and pull
request, using repository secrets for the Azure OpenAI judge credentials. A
failing gate blocks the merge - see that workflow for the exact trigger and
secret configuration.

## Files in this part

```text
part04-observability-evals/
└── python/
    ├── requirements.txt     foundry-local-sdk, azure-ai-evaluation
    ├── eval_pipeline.py      the regression gate: local model responses -> GroundednessEvaluator -> pass/fail
    └── golden_dataset.json   the golden (query, context) cases and the groundedness_threshold to gate on
```

Python only - this part is a CI/CD quality gate, not a language-parity demo
like Parts 1-3, so there's no C# counterpart. `golden_dataset.json` is data,
not code, kept separate from `eval_pipeline.py` so extending the golden set
(see "Extending the golden dataset" above) never means touching the
pipeline logic. `evaluation_results.json` and `_eval_data.jsonl`, written by
a run of `eval_pipeline.py`, are git-ignored the same way the benchmark
charts in Parts 1-2 are - they're this run's output, not repo content.
