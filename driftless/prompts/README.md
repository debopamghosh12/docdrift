# Prompts

LLM prompt templates used by `driftless/analyzer.py` and `driftless/verifier.py`.
All prompts that expect structured output must ask for a single JSON object
(we call the model with `response_format={"type": "json_object"}`).
