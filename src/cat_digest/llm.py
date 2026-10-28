from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from importlib.resources.abc import Traversable

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

LOG = logging.getLogger("cat_digest")
API_ROOT = "https://generativelanguage.googleapis.com/v1beta"
API_URL = API_ROOT + "/models/{model}:generateContent"
PLACEHOLDERS = ("name", "start_date", "end_date")
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class LLMError(RuntimeError):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class LLMConfig:
    prompt: str
    model: str = "gemini-3.8-flash"
    temperature: float = 0.2
    thinking_level: str | None = None
    thinking_levels: dict[str, tuple[str, ...]] = field(default_factory=dict)
    exclude_models: tuple[str, ...] = ()


@dataclass(frozen=True)
class ModelChoice:
    model: str
    level: str | None = None

    @property
    def spec(self) -> str:
        return f"{self.model}/{self.level}" if self.level else self.model

    @property
    def label(self) -> str:
        return f"{self.model} / {self.level} thinking" if self.level else self.model


def parse_model_spec(spec: str) -> ModelChoice:
    model, _, level = spec.strip().removeprefix("models/").partition("/")
    model, level = model.strip(), level.strip()
    if not model:
        raise ValueError("model must look like MODEL or MODEL/THINKING_LEVEL")
    return ModelChoice(model, level or None)


def apply_model_choice(config: LLMConfig, choice: ModelChoice) -> LLMConfig:
    return replace(config, model=choice.model, thinking_level=choice.level)


def user_llm_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "cat-digest" / "llm.toml"


def _read_toml(path: Path | Traversable) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"invalid LLM config {path}: {exc}") from exc


def load_llm_config(path: Path | None = None) -> LLMConfig:
    """Load an explicit file, or the packaged defaults overridden key-by-key by the user file."""
    if path is not None:
        return _parse_llm_config(_read_toml(path), path)
    data = _read_toml(files("cat_digest").joinpath("llm.toml"))
    user_path = user_llm_config_path()
    if user_path.is_file():
        data.update(_read_toml(user_path))
    return _parse_llm_config(data, user_path if user_path.is_file() else "packaged llm.toml")


def _parse_llm_config(data: dict[str, Any], path: Path | str) -> LLMConfig:
    prompt = data.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError(f"{path} must define a non-empty 'prompt'")
    model = data.get("model", LLMConfig.model)
    temperature = data.get("temperature", LLMConfig.temperature)
    if not isinstance(model, str) or not model.strip():
        raise ValueError(f"{path}: 'model' must be a non-empty string")
    if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
        raise ValueError(f"{path}: 'temperature' must be a number")
    level = data.get("thinking_level")
    if level is not None and (not isinstance(level, str) or not level.strip()):
        raise ValueError(f"{path}: 'thinking_level' must be a non-empty string")
    table = data.get("thinking_levels", {})
    if not isinstance(table, dict) or not all(
        isinstance(levels, list) and all(isinstance(item, str) and item for item in levels)
        for levels in table.values()
    ):
        raise ValueError(f"{path}: 'thinking_levels' must map model names to lists of levels")
    excluded = data.get("exclude_models", [])
    if not isinstance(excluded, list) or not all(isinstance(item, str) for item in excluded):
        raise ValueError(f"{path}: 'exclude_models' must be a list of model name patterns")
    return LLMConfig(
        prompt.strip(),
        model.strip(),
        float(temperature),
        level.strip() if level else None,
        {name: tuple(levels) for name, levels in table.items()},
        tuple(excluded),
    )


def build_model_choices(
    models: list[str], levels: dict[str, tuple[str, ...]]
) -> list[ModelChoice]:
    choices = []
    for model in models:
        choices.append(ModelChoice(model))
        choices.extend(ModelChoice(model, level) for level in levels.get(model, ()))
    # A model with levels is only offered with a level, not as a separate no-level entry.
    return [c for c in choices if c.level or not levels.get(c.model)]


def list_models(
    api_key: str,
    levels: dict[str, tuple[str, ...]],
    exclude: tuple[str, ...] = (),
    timeout: float = 30,
) -> list[ModelChoice]:
    """List models that support generateContent, minus excluded patterns, expanded by thinking level."""
    names: list[str] = []
    page_token = None
    while True:
        url = f"{API_ROOT}/models?pageSize=1000"
        if page_token:
            url += f"&pageToken={urllib.parse.quote(page_token)}"
        request = urllib.request.Request(url, headers={"x-goog-api-key": api_key})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            raise LLMError(f"Gemini API error {exc.code} listing models") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise LLMError(f"Could not list models: {getattr(exc, 'reason', exc)}") from exc
        for item in payload.get("models", []):
            name = item["name"].removeprefix("models/")
            if "generateContent" in item.get("supportedGenerationMethods", []) and not any(
                fnmatch.fnmatchcase(name, pattern) for pattern in exclude
            ):
                names.append(name)
        page_token = payload.get("nextPageToken")
        if not page_token:
            break
    return build_model_choices(sorted(dict.fromkeys(names)), levels)


def build_prompt(config: LLMConfig, name: str, start_date: str, end_date: str) -> str:
    values = {"name": name, "start_date": start_date, "end_date": end_date}
    # Only known placeholders are substituted, so other braces in the prompt are safe.
    return re.sub(
        r"\{(%s)\}" % "|".join(PLACEHOLDERS),
        lambda match: values[match.group(1)],
        config.prompt,
    )


def generate_report(
    config: LLMConfig,
    api_key: str,
    system_prompt: str,
    content: str,
    timeout: float = 180,
) -> str:
    body = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": content}]}],
        "generationConfig": {"temperature": config.temperature},
    }
    if config.thinking_level:
        body["generationConfig"]["thinkingConfig"] = {"thinkingLevel": config.thinking_level}
    request = urllib.request.Request(
        API_URL.format(model=config.model),
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise LLMError(
            f"Gemini API error {exc.code}: {detail}", exc.code in RETRYABLE_STATUS
        ) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise LLMError(
            f"Gemini API request failed: {getattr(exc, 'reason', exc)}", True
        ) from exc
    return extract_text(payload)


def extract_text(payload: dict[str, Any]) -> str:
    candidates = payload.get("candidates") or []
    parts = (candidates[0].get("content") or {}).get("parts") if candidates else None
    text = "".join(part.get("text", "") for part in parts or []).strip()
    if not text:
        reason = (
            (candidates[0].get("finishReason") if candidates else None)
            or (payload.get("promptFeedback") or {}).get("blockReason")
            or "no content returned"
        )
        raise LLMError(f"Gemini API returned no text ({reason})")
    return text + "\n"


def report_filename(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-") or "report"
    return f"{slug}.txt"
