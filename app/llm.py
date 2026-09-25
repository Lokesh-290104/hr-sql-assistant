"""LLM clients. The pipeline depends only on the LLMClient protocol, so providers are swappable."""

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Protocol

from app.config import settings


logger = logging.getLogger("hr_assistant")


class LLMError(RuntimeError):
    pass


class LLMClient(Protocol):
    def generate(self, system: str, user: str) -> str: ...


@dataclass
class LLMAnswer:
    sql: str | None
    explanation: str
    clarification: str | None


class GeminiClient:
    """Gemini with failover across models.

    - 500/503/504 (overloaded): retry the same model once with backoff, then move on.
    - 429 (quota exhausted), 404 (model gone), network errors/timeouts: move to the next model.
    - The first model that answers becomes the preferred one for later calls.
    - A total deadline bounds how long one generate() call can take.
    """

    RETRY_SAME_MODEL_CODES = {500, 503, 504}
    RETRIES_PER_MODEL = 1

    def __init__(self, api_key: str | None = None, model: str | None = None):
        from google import genai

        api_key = api_key or settings.gemini_api_key
        if not api_key:
            raise LLMError("GEMINI_API_KEY is not set. Add it to your .env file.")
        self._client = genai.Client(
            api_key=api_key, http_options=genai.types.HttpOptions(timeout=settings.llm_timeout_ms)
        )
        self._types = genai.types
        self._api_error = genai.errors.APIError
        self.models = [model or settings.gemini_model] + [
            m for m in settings.gemini_fallback_models if m != (model or settings.gemini_model)
        ]
        self._preferred = self.models[0]
        self._lock = threading.Lock()

    @property
    def active_model(self) -> str:
        return self._preferred

    def _ordered_models(self) -> list[str]:
        with self._lock:
            preferred = self._preferred
        return [preferred] + [m for m in self.models if m != preferred]

    def generate(self, system: str, user: str) -> str:
        def config(timeout_ms: int):
            return self._types.GenerateContentConfig(
                system_instruction=system,
                temperature=0,
                response_mime_type="application/json",
                automatic_function_calling=self._types.AutomaticFunctionCallingConfig(disable=True),
                http_options=self._types.HttpOptions(timeout=timeout_ms),
            )

        deadline = time.monotonic() + settings.llm_deadline_ms / 1000
        failures: list[str] = []
        for model in self._ordered_models():
            for attempt in range(self.RETRIES_PER_MODEL + 1):
                remaining_ms = int((deadline - time.monotonic()) * 1000)
                if remaining_ms < 1000:
                    raise LLMError(_failure_message(failures, timed_out=True))
                try:
                    response = self._client.models.generate_content(
                        model=model, contents=user, config=config(min(settings.llm_timeout_ms, remaining_ms))
                    )
                except self._api_error as e:
                    failures.append(f"{model}: {e.code}")
                    if e.code in self.RETRY_SAME_MODEL_CODES and attempt < self.RETRIES_PER_MODEL:
                        time.sleep(2**attempt)
                        continue
                    break
                except Exception as e:  # timeouts and network errors: try the next model
                    failures.append(f"{model}: {type(e).__name__}")
                    break
                with self._lock:
                    self._preferred = model
                return response.text or ""
        raise LLMError(_failure_message(failures))


def _failure_message(failures: list[str], timed_out: bool = False) -> str:
    if any(f.endswith(": 429") for f in failures):
        reason = "today's free Gemini quota is used up"
    elif timed_out:
        reason = "Gemini took too long to respond"
    else:
        reason = "Gemini is unavailable right now"
    logger.warning("Gemini failed (%s): %s", reason, ", ".join(failures))
    return f"The AI service couldn't answer because {reason}. Please try again later."


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def parse_answer(raw: str) -> LLMAnswer:
    """Parse the model's JSON reply, tolerating markdown code fences."""
    text = _FENCE.sub("", raw.strip())
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise LLMError("The model did not return valid JSON.") from None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            raise LLMError("The model did not return valid JSON.") from None
    if not isinstance(data, dict):
        raise LLMError("The model did not return a JSON object.")

    sql = data.get("sql")
    sql = sql.strip() if isinstance(sql, str) and sql.strip() else None
    clarification = data.get("clarification")
    clarification = clarification.strip() if isinstance(clarification, str) and clarification.strip() else None
    explanation = str(data.get("explanation") or "").strip()
    if sql is None and clarification is None and not explanation:
        raise LLMError("The model returned an empty answer.")
    return LLMAnswer(sql=sql, explanation=explanation, clarification=clarification)
