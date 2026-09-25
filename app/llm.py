"""LLM clients. The pipeline depends only on the LLMClient protocol, so providers are swappable."""

import json
import re
import time
from dataclasses import dataclass
from typing import Protocol

from app.config import settings


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
    # Rate limits and overload are temporary: retry with backoff, then try the next model.
    RETRYABLE_CODES = {429, 500, 503, 504}
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
        self.model = model or settings.gemini_model
        self.models = [self.model] + [m for m in settings.gemini_fallback_models if m != self.model]

    def generate(self, system: str, user: str) -> str:
        config = self._types.GenerateContentConfig(
            system_instruction=system,
            temperature=0,
            response_mime_type="application/json",
            automatic_function_calling=self._types.AutomaticFunctionCallingConfig(disable=True),
        )
        last_error: Exception | None = None
        for model in self.models:
            for attempt in range(self.RETRIES_PER_MODEL + 1):
                try:
                    response = self._client.models.generate_content(model=model, contents=user, config=config)
                    if model != self.models[0]:
                        # Stick with the model that works instead of waiting on the overloaded one again.
                        self.models.remove(model)
                        self.models.insert(0, model)
                    return response.text or ""
                except self._api_error as e:
                    last_error = e
                    if e.code not in self.RETRYABLE_CODES:
                        break  # e.g. 404 model not found: skip straight to the next model
                    if attempt < self.RETRIES_PER_MODEL:
                        time.sleep(2**attempt)
                except Exception as e:  # network errors etc.
                    raise LLMError(f"Gemini request failed: {e}") from e
        raise LLMError(f"Gemini request failed: {last_error}")


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
    return LLMAnswer(sql=sql, explanation=str(data.get("explanation") or "").strip(), clarification=clarification)
