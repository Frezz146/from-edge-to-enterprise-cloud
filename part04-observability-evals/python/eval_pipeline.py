"""Regression gate for the agent built in Parts 1-3.

For every case in golden_dataset.json, generate a response with the local
Foundry Local model (constrained to the case's context, RAG-style), then
score how grounded that response is in that context using Azure AI
Evaluation's GroundednessEvaluator (an LLM judge running against an Azure
OpenAI deployment).

This is the check a model update or prompt change has to pass before it
ships: if the mean groundedness score across the golden dataset drops below
`groundedness_threshold`, the pipeline exits non-zero so a CI job can block
the change. See ../README.md and the GitHub Actions workflow in
.github/workflows/eval.yml for how this plugs into CI/CD.

Required environment variables:
  AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY, AZURE_OPENAI_DEPLOYMENT
    -> the judge model used by GroundednessEvaluator.
"""

import json
import os
import sys
from pathlib import Path

from azure.ai.evaluation import GroundednessEvaluator, evaluate
from foundry_local_sdk import Configuration, FoundryLocalManager

MODEL_ALIAS = "qwen2.5-0.5b"
APP_NAME = "from_edge_to_enterprise_eval_pipeline"
DATASET_PATH = Path(__file__).parent / "golden_dataset.json"
EVAL_DATA_PATH = Path(__file__).parent / "_eval_data.jsonl"
RESULTS_PATH = Path(__file__).parent / "evaluation_results.json"


def generate_responses(cases: list[dict]) -> None:
    """Runs each case's query through the local model, grounded in its
    context, and writes {query, context, response} rows for evaluate()."""
    config = Configuration(app_name=APP_NAME)
    FoundryLocalManager.initialize(config)
    manager = FoundryLocalManager.instance
    manager.download_and_register_eps()

    model = manager.catalog.get_model(MODEL_ALIAS)
    model.download(lambda progress: None)
    model.load()
    client = model.get_chat_client()

    with open(EVAL_DATA_PATH, "w", encoding="utf-8") as f:
        for case in cases:
            messages = [
                {
                    "role": "system",
                    "content": (
                        "Answer the user's question using only the information in "
                        "the provided context. Do not add facts that aren't in it.\n\n"
                        f"Context: {case['context']}"
                    ),
                },
                {"role": "user", "content": case["query"]},
            ]
            response = client.complete_chat(messages)
            answer = response.choices[0].message.content
            print(f"  [{case['id']}] {case['query']}\n    -> {answer}\n")

            f.write(
                json.dumps(
                    {
                        "id": case["id"],
                        "query": case["query"],
                        "context": case["context"],
                        "response": answer,
                    }
                )
                + "\n"
            )

    model.unload()


def find_metric(d: dict, needle: str) -> float | None:
    """SDK versions have renamed groundedness metric keys before (e.g.
    "groundedness" vs "gpt_groundedness") - match by substring instead of a
    single hardcoded key so this pipeline doesn't silently stop gating on a
    version bump."""
    for key, value in d.items():
        if needle in key.lower() and isinstance(value, (int, float)):
            return float(value)
    return None


def main() -> int:
    dataset = json.loads(DATASET_PATH.read_text())
    threshold = dataset["groundedness_threshold"]

    print(f"[Local] Generating responses for {len(dataset['cases'])} golden case(s)...")
    generate_responses(dataset["cases"])

    model_config = {
        "azure_endpoint": os.environ["AZURE_OPENAI_ENDPOINT"],
        "api_key": os.environ["AZURE_OPENAI_API_KEY"],
        "azure_deployment": os.environ["AZURE_OPENAI_DEPLOYMENT"],
    }
    groundedness_evaluator = GroundednessEvaluator(model_config=model_config)

    print("[Cloud] Scoring groundedness with Azure AI Evaluation...")
    result = evaluate(
        data=str(EVAL_DATA_PATH),
        evaluators={"groundedness": groundedness_evaluator},
        evaluator_config={
            "groundedness": {
                "column_mapping": {
                    "query": "${data.query}",
                    "context": "${data.context}",
                    "response": "${data.response}",
                }
            }
        },
        output_path=str(RESULTS_PATH),
    )

    mean_score = find_metric(result.get("metrics", {}), "groundedness")
    print(f"\nMean groundedness score: {mean_score} (threshold: {threshold})")

    passed = mean_score is not None and mean_score >= threshold
    print("PASS" if passed else "FAIL", "- results written to", RESULTS_PATH)

    EVAL_DATA_PATH.unlink(missing_ok=True)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
