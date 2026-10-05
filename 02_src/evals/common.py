"""Helpers shared by the evaluation of every agent (LangSmith datasets, LLM-as-a-Judge, number checks)."""

import json
import os
import re

from openevals.llm import create_llm_as_judge


def model_name() -> str:
    """The one model every agent uses (OPENAI_MODEL); the judges use it too."""
    return os.getenv("OPENAI_MODEL", "gpt-5.6-luna")


def judge_llm():
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(model=model_name(), use_responses_api=True)


JUDGE_PROMPT = """You are an expert grader of the output of an AI agent in a restaurant-marketing product.

Criterion: {rubric}

Grade only this criterion, using the material below as the only source of truth. Give 1.0 when the criterion is fully
met and lower scores for each problem you find. Explain briefly what you found.

<input>
{{inputs}}
</input>

<output>
{{outputs}}
</output>

<reference>
{{reference_outputs}}
</reference>
"""


def make_judge(key, rubric, judge, *, input_of=lambda inputs: inputs, output_of=lambda outputs: outputs):
    """An LLM-as-a-Judge evaluator that scores one criterion between 0 and 1 (with its reasoning)."""
    grade = create_llm_as_judge(
        prompt=JUDGE_PROMPT.format(rubric=rubric), feedback_key=key, judge=judge, continuous=True, use_reasoning=True,
    )

    def evaluator(inputs: dict, outputs: dict, reference_outputs: dict) -> dict:
        if outputs.get("error"):
            return {"key": key, "score": 0, "comment": f"the agent failed: {outputs['error']}"}
        return grade(inputs=input_of(inputs), outputs=output_of(outputs), reference_outputs=reference_outputs)

    evaluator.__name__ = key
    return evaluator


def result(key: str, score: float, comment: str = "") -> dict:
    return {"key": key, "score": score, "comment": comment or "ok"}


_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def normalize_number(text: str) -> str:
    """'10.71' -> '10.71', '0.0' -> '0', '1,234' -> '1234', so the same quantity compares equal."""
    return format(float(text.replace(",", "")), "g")


def numbers_in(text) -> set[str]:
    """Every number that appears in a string (or in the JSON of any value)."""
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False)
    return {normalize_number(match) for match in _NUMBER.findall(text)}


def upsert_examples(client, dataset_name: str, description: str, examples: list[dict]) -> tuple[int, int]:
    """Create the dataset if needed and add the examples it does not have yet (matched by metadata.key)."""
    if client.has_dataset(dataset_name=dataset_name):
        dataset = client.read_dataset(dataset_name=dataset_name)
    else:
        dataset = client.create_dataset(dataset_name, description=description)

    def key_of(metadata):
        metadata = metadata or {}
        # Strategy examples uploaded earlier carried only the qualification run id.
        return metadata.get("key") or (f"qualification_run_{metadata['qualification_run_id']}" if "qualification_run_id" in metadata else None)

    known = {key_of(example.metadata) for example in client.list_examples(dataset_id=dataset.id)}
    new = [example for example in examples if example["key"] not in known]
    if new:
        client.create_examples(
            dataset_id=dataset.id,
            examples=[
                {"inputs": e["inputs"], "outputs": e["outputs"], "metadata": {**e.get("metadata", {}), "key": e["key"]}}
                for e in new
            ],
        )
    return len(new), len(examples) - len(new)
