"""Validate the JSON-schema subset used by the extractors and count usage."""

import json
import math
import sys


def matches_schema(value, schema):
    """Local backstop: constrained generation can still be blocked/truncated.

    Supports the keywords used in this repository, not arbitrary JSON Schema.
    """
    if "anyOf" in schema:
        return any(matches_schema(value, option) for option in schema["anyOf"])
    kinds = schema.get("type", [])
    if isinstance(kinds, str):
        kinds = [kinds]
    actual = ("null" if value is None else "boolean" if isinstance(value, bool)
              else "number" if isinstance(value, (int, float))
              else "string" if isinstance(value, str)
              else "array" if isinstance(value, list)
              else "object" if isinstance(value, dict) else "invalid")
    if kinds and actual not in kinds:
        return False
    if actual == "number" and not math.isfinite(value):
        return False
    if "enum" in schema and value not in schema["enum"]:
        return False
    if actual == "object":
        props = schema.get("properties", {})
        if not set(schema.get("required", [])).issubset(value):
            return False
        if schema.get("additionalProperties") is False and set(value) - set(props):
            return False
        return all(matches_schema(v, props[k]) for k, v in value.items() if k in props)
    if actual == "array":
        return all(matches_schema(v, schema.get("items", {})) for v in value)
    return True


def parse_response(resp, schema, transform=lambda value: value):
    try:
        value = transform(json.loads(resp.text))
        if matches_schema(value, schema):
            return value
    except (ValueError, TypeError):
        pass
    print("  [llm response] missing, truncated, or invalid structured output", file=sys.stderr)
    return None


def token_usage(resp):
    """Tool results are additional input (not included in prompt_token_count).

    Especially relevant for AGENTIC video. Missing metadata is unknown, not a
    reason to throw away a valid analysis; make the telemetry gap visible.
    """
    usage = getattr(resp, "usage_metadata", None)
    if usage is None:
        print("  [usage warning] Gemini omitted usage metadata", file=sys.stderr)
        return 0, 0
    return ((getattr(usage, "prompt_token_count", None) or 0)
            + (getattr(usage, "tool_use_prompt_token_count", None) or 0),
            (getattr(usage, "candidates_token_count", None) or 0)
            + (getattr(usage, "thoughts_token_count", None) or 0))
