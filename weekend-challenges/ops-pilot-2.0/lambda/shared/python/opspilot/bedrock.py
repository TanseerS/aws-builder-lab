"""Amazon Bedrock access via the Converse API.

Deliberately provider-neutral: only ``converse`` is used, so changing
``BEDROCK_MODEL_ID`` to any other Bedrock text-generation model keeps working.
The model is never given tools, AWS credentials or the ability to act - it
receives normalised evidence and returns JSON.
"""

from __future__ import annotations

import json
import random
import re
import time
from typing import Any, Final

from botocore.exceptions import BotoCoreError, ClientError

from . import config
from .aws_clients import client
from .logging_utils import get_logger

log = get_logger("bedrock")

#: Bedrock error codes worth retrying with backoff.
_RETRYABLE: Final[frozenset[str]] = frozenset(
    {
        "ThrottlingException",
        "TooManyRequestsException",
        "ServiceUnavailableException",
        "InternalServerException",
        "ModelTimeoutException",
        "ModelNotReadyException",
    }
)

_FENCE_RE = re.compile(r"^\s*```(?:json|JSON)?\s*|\s*```\s*$")


class BedrockUnavailable(RuntimeError):
    """Raised when Bedrock cannot produce a usable response."""


def converse(
    prompt: str,
    system_prompt: str | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
) -> str:
    """Call Bedrock Converse with bounded retries, returning the raw text.

    Raises :class:`BedrockUnavailable` once retries are exhausted so callers can
    fall back deterministically rather than inventing an analysis.
    """
    body: dict[str, Any] = {
        "modelId": config.BEDROCK_MODEL_ID,
        "messages": [{"role": "user", "content": [{"text": prompt}]}],
        "inferenceConfig": {
            "maxTokens": max_tokens or config.BEDROCK_MAX_TOKENS,
            "temperature": (
                temperature if temperature is not None else config.BEDROCK_TEMPERATURE
            ),
        },
    }
    if system_prompt:
        body["system"] = [{"text": system_prompt}]

    last_error: Exception | None = None
    for attempt in range(1, config.BEDROCK_MAX_ATTEMPTS + 1):
        started = time.time()
        try:
            response = client("bedrock-runtime").converse(**body)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "Unknown")
            last_error = exc
            if code not in _RETRYABLE or attempt == config.BEDROCK_MAX_ATTEMPTS:
                log.error(
                    "bedrock_call_failed",
                    model_id=config.BEDROCK_MODEL_ID,
                    error_code=code,
                    attempt=attempt,
                    retryable=code in _RETRYABLE,
                )
                raise BedrockUnavailable(f"Bedrock error {code}") from exc
            _sleep_backoff(attempt, code)
        except BotoCoreError as exc:  # connection/timeout level failure
            last_error = exc
            if attempt == config.BEDROCK_MAX_ATTEMPTS:
                log.error(
                    "bedrock_transport_failed",
                    error_type=type(exc).__name__,
                    attempt=attempt,
                )
                raise BedrockUnavailable("Bedrock transport failure") from exc
            _sleep_backoff(attempt, type(exc).__name__)
        else:
            latency_ms = int((time.time() - started) * 1000)
            usage = response.get("usage", {})
            log.info(
                "bedrock_call_succeeded",
                model_id=config.BEDROCK_MODEL_ID,
                latency_ms=latency_ms,
                attempt=attempt,
                input_tokens=usage.get("inputTokens"),
                output_tokens=usage.get("outputTokens"),
                stop_reason=response.get("stopReason"),
            )
            return _extract_text(response)

    raise BedrockUnavailable("Bedrock retries exhausted") from last_error


def _sleep_backoff(attempt: int, reason: str) -> None:
    """Exponential backoff with jitter between Bedrock retries."""
    delay = config.BEDROCK_BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
    delay += random.uniform(0, 0.4)
    log.warning("bedrock_retry", attempt=attempt, reason=reason, sleep_seconds=round(delay, 2))
    time.sleep(min(delay, 10.0))


def _extract_text(response: dict[str, Any]) -> str:
    """Pull the assistant text out of a Converse response."""
    blocks = response.get("output", {}).get("message", {}).get("content", [])
    parts = [b["text"] for b in blocks if isinstance(b, dict) and isinstance(b.get("text"), str)]
    text = "\n".join(parts).strip()
    if not text:
        raise BedrockUnavailable("Bedrock returned an empty response")
    return text


# --- Resilient JSON parsing ---------------------------------------------------
def strip_code_fences(text: str) -> str:
    """Remove Markdown code fences that models routinely add around JSON."""
    cleaned = text.strip()
    if "```" not in cleaned:
        return cleaned
    # Prefer the contents of the first fenced block.
    fenced = re.search(r"```(?:json|JSON)?\s*(.*?)```", cleaned, re.DOTALL)
    if fenced:
        return fenced.group(1).strip()
    return _FENCE_RE.sub("", cleaned).strip()


def extract_json_object(text: str) -> str | None:
    """Return the outermost balanced ``{...}`` span, ignoring braces in strings."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def parse_json_response(text: str) -> dict[str, Any] | None:
    """Parse model output into a dict, tolerating common formatting defects.

    Handles: Markdown fences, prose wrapped around the object, trailing commas,
    and single-quoted keys. Returns None rather than raising - a malformed model
    response must never break the incident workflow.
    """
    if not text or not text.strip():
        return None

    candidates: list[str] = []
    unfenced = strip_code_fences(text)
    candidates.append(unfenced)

    extracted = extract_json_object(unfenced)
    if extracted and extracted != unfenced:
        candidates.append(extracted)

    repaired = [_repair_json(c) for c in candidates]
    for candidate in candidates + repaired:
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
            return parsed[0]

    log.warning("bedrock_json_parse_failed", preview=text[:300])
    return None


def _repair_json(text: str) -> str:
    """Apply conservative fixes for the JSON defects small models emit."""
    if not text:
        return text
    repaired = re.sub(r",\s*([}\]])", r"\1", text)          # trailing commas
    repaired = re.sub(r"\bNaN\b", "null", repaired)
    repaired = re.sub(r"\b(Infinity|-Infinity)\b", "null", repaired)
    repaired = re.sub(r"\b(True|False|None)\b", lambda m: {
        "True": "true", "False": "false", "None": "null"
    }[m.group(1)], repaired)
    return repaired
