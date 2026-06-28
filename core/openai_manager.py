from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - handled at runtime for clearer UX.
    load_dotenv = None  # type: ignore[assignment]

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover - handled at runtime for clearer UX.
    OpenAI = None  # type: ignore[assignment]


LOGGER = logging.getLogger(__name__)


class OpenAIManagerError(RuntimeError):
    """Raised when the OpenAI integration cannot complete a request."""


@dataclass(frozen=True)
class AIResponse:
    text: str
    emotion: str = "neutral"
    raw_text: str = ""
    model: str = ""
    from_cache: bool = False


class OpenAIManager:
    """OpenAI API facade prepared for text plus emotion responses."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._client: Any | None = None
        self._api_key: str = ""

    def generate_response(
        self,
        *,
        history: list[dict[str, str]],
        model: str,
        temperature: float,
        max_response_words: int,
        reasoning_effort: str,
        system_prompt: str,
    ) -> AIResponse:
        client = self._get_client()
        input_messages = [
            {"role": message["role"], "content": message["content"]}
            for message in history
            if message.get("role") in {"user", "assistant"}
            and message.get("content", "").strip()
        ]
        if not input_messages:
            raise OpenAIManagerError("No user message was provided.")

        LOGGER.info("Sending OpenAI request with model=%s", model)
        request_payload: dict[str, Any] = {
            "model": model,
            "input": input_messages,
            "instructions": system_prompt,
            "max_output_tokens": self._estimate_max_output_tokens(max_response_words),
        }
        if self._model_supports_temperature(model):
            request_payload["temperature"] = temperature
        else:
            LOGGER.info("Skipping temperature for model=%s", model)
        if self._model_supports_reasoning(model) and reasoning_effort.strip():
            request_payload["reasoning"] = {"effort": reasoning_effort.strip()}

        response = self._create_response_with_fallback(client, request_payload, model)

        raw_text = self._extract_text(response).strip()
        if not raw_text:
            raise OpenAIManagerError("OpenAI returned an empty response.")

        parsed = self._parse_response_text(raw_text)
        text = self._trim_text_to_word_limit(parsed["text"], max_response_words)
        return AIResponse(
            text=text,
            emotion=parsed["emotion"],
            raw_text=raw_text,
            model=model,
            from_cache=False,
        )

    def _create_response_with_fallback(
        self,
        client: Any,
        request_payload: dict[str, Any],
        model: str,
    ) -> Any:
        optional_params = {"temperature", "reasoning", "max_output_tokens"}
        while True:
            try:
                return client.responses.create(**request_payload)
            except Exception as exc:
                rejected_param = self._unsupported_parameter_name(exc)
                if (
                    rejected_param in optional_params
                    and rejected_param in request_payload
                ):
                    LOGGER.warning(
                        "Model %s rejected %s; retrying without it",
                        model,
                        rejected_param,
                    )
                    request_payload.pop(rejected_param, None)
                    continue

                LOGGER.exception("OpenAI request failed")
                raise OpenAIManagerError(f"OpenAI request failed: {exc}") from exc

    def _get_client(self) -> Any:
        if load_dotenv is None:
            raise OpenAIManagerError(
                "python-dotenv is not installed. Run pip install -r requirements.txt."
            )
        if OpenAI is None:
            raise OpenAIManagerError(
                "openai is not installed. Run pip install -r requirements.txt."
            )

        load_dotenv(self.root / ".env")
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise OpenAIManagerError("OPENAI_API_KEY is missing in .env.")

        if self._client is None or self._api_key != api_key:
            self._client = OpenAI(api_key=api_key)
            self._api_key = api_key
        return self._client

    def _extract_text(self, response: Any) -> str:
        output_text = getattr(response, "output_text", None)
        if isinstance(output_text, str) and output_text:
            return output_text

        fragments: list[str] = []
        for item in getattr(response, "output", []) or []:
            for content in getattr(item, "content", []) or []:
                text = getattr(content, "text", None)
                if isinstance(text, str):
                    fragments.append(text)
        return "\n".join(fragments)

    def _parse_response_text(self, raw_text: str) -> dict[str, str]:
        payload = self._load_json_object(raw_text)
        if isinstance(payload, dict):
            text = str(payload.get("text", "")).strip()
            emotion = str(payload.get("emotion", "neutral")).strip() or "neutral"
            if text:
                return {"text": text, "emotion": emotion}

        return {"text": raw_text.strip(), "emotion": "neutral"}

    def _load_json_object(self, raw_text: str) -> Any:
        try:
            return json.loads(raw_text)
        except json.JSONDecodeError:
            start = raw_text.find("{")
            end = raw_text.rfind("}")
            if start == -1 or end == -1 or end <= start:
                return None
            try:
                return json.loads(raw_text[start : end + 1])
            except json.JSONDecodeError:
                return None

    def _model_supports_temperature(self, model: str) -> bool:
        model_id = model.strip().lower()
        if model_id.startswith("gpt-5"):
            return False
        if len(model_id) > 1 and model_id[0] == "o" and model_id[1].isdigit():
            return False
        return True

    def _model_supports_reasoning(self, model: str) -> bool:
        model_id = model.strip().lower()
        return model_id.startswith("gpt-5") or (
            len(model_id) > 1 and model_id[0] == "o" and model_id[1].isdigit()
        )

    def _unsupported_parameter_name(self, exc: Exception) -> str:
        message = str(exc).lower()
        if "unsupported parameter" not in message:
            return ""
        for parameter in ("temperature", "reasoning", "max_output_tokens"):
            if parameter in message:
                return parameter
        return ""

    def _estimate_max_output_tokens(self, max_response_words: int) -> int:
        clean_limit = max(20, min(250, int(max_response_words)))
        return max(100, min(900, clean_limit * 3 + 80))

    def _trim_text_to_word_limit(self, text: str, max_response_words: int) -> str:
        clean_limit = max(20, min(250, int(max_response_words)))
        words = text.split()
        if len(words) <= clean_limit:
            return text

        trimmed = " ".join(words[:clean_limit]).rstrip(" ,;:")
        if trimmed and trimmed[-1] not in ".!?":
            trimmed += "."
        return trimmed
