"""Gemini client wrapper. Every agent stage should call the LLM through here,
never import google.genai directly, so cost/latency tracking stays centralized.
"""

from dataclasses import dataclass
from time import perf_counter

from app.config import get_settings

# Approximate Gemini 2.5 Pro pricing (USD per 1M tokens). Update as pricing changes.
_PRICE_PER_M_INPUT = 1.25
_PRICE_PER_M_OUTPUT = 5.00


@dataclass
class LLMResponse:
    text: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float
    cost_usd: float


class GeminiClient:
    def __init__(self, model: str | None = None):
        settings = get_settings()
        self.model = model or settings.gemini_model
        self._settings = settings
        self._client = None  # lazily initialized real client, see _get_client

    def _get_client(self):
        if self._client is None:
            import google.genai as genai

            self._client = genai.Client(
                vertexai=True,
                project=self._settings.gcp_project_id,
                location=self._settings.gcp_location,
            )
        return self._client

    def generate(self, prompt: str, *, system_instruction: str | None = None) -> LLMResponse:
        start = perf_counter()
        client = self._get_client()

        response = client.models.generate_content(
            model=self.model,
            contents=prompt,
            config={"system_instruction": system_instruction} if system_instruction else None,
        )

        latency_ms = (perf_counter() - start) * 1000
        usage = getattr(response, "usage_metadata", None)
        prompt_tokens = getattr(usage, "prompt_token_count", 0) or 0
        completion_tokens = getattr(usage, "candidates_token_count", 0) or 0
        cost_usd = (
            prompt_tokens / 1_000_000 * _PRICE_PER_M_INPUT
            + completion_tokens / 1_000_000 * _PRICE_PER_M_OUTPUT
        )

        return LLMResponse(
            text=response.text,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
            cost_usd=cost_usd,
        )
