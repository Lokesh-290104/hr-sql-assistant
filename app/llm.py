"""LLM clients. The pipeline depends only on the LLMClient protocol, so providers are swappable."""

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Protocol

import httpx

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


class ProviderError(Exception):
    """An HTTP-level failure from an LLM provider (status code in .code)."""

    def __init__(self, code: int, message: str = ""):
        super().__init__(f"HTTP {code} {message}".strip())
        self.code = code


class FailoverClient:
    """Shared retry/failover policy for every provider.

    - 500/502/503/504 (overloaded): retry the same model once with backoff, then move on.
    - 429 (quota/rate limit), 402 (no credits), 404 (model gone), network errors/timeouts:
      move straight to the next model.
    - The first model that answers becomes the preferred one for later calls.
    - A total deadline bounds how long one generate() call can take.
    Subclasses implement _call() for one request to one model.
    """

    provider = "LLM"
    RETRY_SAME_MODEL_CODES = {500, 502, 503, 504}
    RETRIES_PER_MODEL = 1

    def __init__(self, models: list[str]):
        if not models:
            raise LLMError(f"No {self.provider} models configured.")
        self.models = list(dict.fromkeys(models))  # de-duplicate, keep order
        self._preferred = self.models[0]
        self._lock = threading.Lock()

    def _call(self, model: str, system: str, user: str, timeout_ms: int) -> str:
        raise NotImplementedError

    @property
    def active_model(self) -> str:
        return self._preferred

    def _ordered_models(self) -> list[str]:
        with self._lock:
            preferred = self._preferred
        return [preferred] + [m for m in self.models if m != preferred]

    def generate(self, system: str, user: str) -> str:
        deadline = time.monotonic() + settings.llm_deadline_ms / 1000
        failures: list[str] = []
        for model in self._ordered_models():
            for attempt in range(self.RETRIES_PER_MODEL + 1):
                remaining_ms = int((deadline - time.monotonic()) * 1000)
                if remaining_ms < 1000:
                    raise LLMError(self._failure_message(failures, timed_out=True))
                try:
                    text = self._call(model, system, user, min(settings.llm_timeout_ms, remaining_ms))
                except ProviderError as e:
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
                return text
        raise LLMError(self._failure_message(failures))

    def _failure_message(self, failures: list[str], timed_out: bool = False) -> str:
        codes = {f.rsplit(": ", 1)[-1] for f in failures}
        if "402" in codes:
            reason = f"the {self.provider} account is out of credits"
        elif "429" in codes:
            reason = f"the {self.provider} usage limit has been reached for now"
        elif "401" in codes or "403" in codes:
            reason = f"the {self.provider} API key was rejected"
        elif timed_out:
            reason = f"{self.provider} took too long to respond"
        else:
            reason = f"{self.provider} is unavailable right now"
        logger.warning("%s failed (%s): %s", self.provider, reason, ", ".join(failures))
        return f"The AI service couldn't answer because {reason}. Please try again later."


class GeminiClient(FailoverClient):
    provider = "Gemini"

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
        super().__init__([model or settings.gemini_model, *settings.gemini_fallback_models])

    def _call(self, model: str, system: str, user: str, timeout_ms: int) -> str:
        config = self._types.GenerateContentConfig(
            system_instruction=system,
            temperature=0,
            response_mime_type="application/json",
            automatic_function_calling=self._types.AutomaticFunctionCallingConfig(disable=True),
            http_options=self._types.HttpOptions(timeout=timeout_ms),
        )
        try:
            response = self._client.models.generate_content(model=model, contents=user, config=config)
        except self._api_error as e:
            raise ProviderError(e.code) from e
        return response.text or ""


class OpenRouterClient(FailoverClient):
    """OpenRouter's OpenAI-compatible chat API: one key, hundreds of models."""

    provider = "OpenRouter"
    URL = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self, api_key: str | None = None, models: list[str] | None = None, http: httpx.Client | None = None):
        api_key = api_key or settings.openrouter_api_key
        if not api_key:
            raise LLMError("OPENROUTER_API_KEY is not set. Add it to your .env file.")
        self._http = http or httpx.Client()
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            # Optional attribution headers OpenRouter uses for its app rankings.
            "HTTP-Referer": "http://localhost:8000",
            "X-Title": "HR Query Assistant",
        }
        super().__init__(models or settings.openrouter_models)

    def _call(self, model: str, system: str, user: str, timeout_ms: int) -> str:
        response = self._http.post(
            self.URL,
            headers=self._headers,
            timeout=timeout_ms / 1000,
            json={
                "model": model,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            },
        )
        if response.status_code != 200:
            raise ProviderError(response.status_code, response.text[:200])
        data = response.json()
        if "error" in data:  # some upstream failures arrive as 200 with an error body
            raise ProviderError(int(data["error"].get("code") or 502), str(data["error"].get("message", ""))[:200])
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise ProviderError(502, "unexpected response shape") from None


def create_llm_client() -> FailoverClient:
    """The provider named by LLM_PROVIDER in .env."""
    provider = settings.llm_provider
    if provider == "openrouter":
        return OpenRouterClient()
    if provider == "gemini":
        return GeminiClient()
    raise LLMError(f"Unknown LLM_PROVIDER '{provider}'. Use 'gemini' or 'openrouter'.")


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
