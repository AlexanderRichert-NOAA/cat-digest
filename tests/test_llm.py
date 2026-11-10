import io
import json

import pytest

from cat_digest import llm
from cat_digest.llm import (
    LLMConfig,
    build_prompt,
    extract_text,
    generate_report,
    load_llm_config,
    report_filename,
)


def test_load_config_and_build_prompt(tmp_path):
    path = tmp_path / "llm.toml"
    path.write_text('model = "m"\nprompt = "Summarize {name} {start_date}-{end_date} {other}"\n')

    config = load_llm_config(path)

    assert config.model == "m"
    assert build_prompt(config, "Ann", "2026-01-01", "2026-01-31") == (
        "Summarize Ann 2026-01-01-2026-01-31 {other}"
    )


def test_load_config_requires_prompt(tmp_path):
    path = tmp_path / "llm.toml"
    path.write_text('model = "m"\n')

    with pytest.raises(ValueError, match="prompt"):
        load_llm_config(path)


def test_generate_report_sends_key_in_header_and_returns_text(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["key"] = request.get_header("X-goog-api-key")
        seen["body"] = json.loads(request.data)
        return io.BytesIO(json.dumps(
            {"candidates": [{"content": {"parts": [{"text": "Summary"}]}}]}
        ).encode())

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)

    result = generate_report(LLMConfig("p", "model-x"), "secret", "system", "raw")

    assert result == "Summary\n"
    assert seen["url"].endswith("/models/model-x:generateContent")
    assert "secret" not in seen["url"] and seen["key"] == "secret"
    assert seen["body"]["systemInstruction"]["parts"][0]["text"] == "system"
    assert seen["body"]["contents"][0]["parts"][0]["text"] == "raw"


def test_generate_report_saves_prompt_before_each_attempt(tmp_path, monkeypatch, caplog):
    import logging

    caplog.set_level(logging.DEBUG, logger="cat_digest")
    request_path = tmp_path / "Ann-One-llm-prompt.txt"
    saved = []

    def fake_urlopen(request, timeout):
        path = request_path if not saved else tmp_path / "Ann-One-llm-prompt-2.txt"
        assert path.read_text(encoding="utf-8") == "system\n\nraw"
        body = json.loads(request.data)
        assert body["systemInstruction"]["parts"][0]["text"] == "system"
        assert body["contents"][0]["parts"][0]["text"] == "raw"
        saved.append(path)
        if len(saved) == 1:
            raise llm.urllib.error.URLError("offline")
        return io.BytesIO(json.dumps(
            {"candidates": [{"content": {"parts": [{"text": "Summary"}]}}]}
        ).encode())

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    config = LLMConfig("p", "model-x", thinking_level="high")
    with pytest.raises(llm.LLMError, match="offline"):
        generate_report(config, "secret", "system", "raw", request_path=request_path)
    result = generate_report(config, "secret", "system", "raw", request_path=request_path)

    assert result == "Summary\n"
    assert len(saved) == 2
    assert "model-x" in caplog.text
    assert '"temperature": 0.2' in caplog.text
    assert '"thinkingLevel": "high"' in caplog.text
    assert "secret" not in caplog.text
    assert "secret" not in request_path.read_text()


def test_generate_report_does_not_send_if_request_cannot_be_saved(tmp_path, monkeypatch):
    monkeypatch.setattr(
        llm.urllib.request, "urlopen",
        lambda *args, **kwargs: pytest.fail("request sent before saving"),
    )
    existing_file = tmp_path / "not-a-directory"
    existing_file.write_text("unchanged")
    with pytest.raises(OSError):
        generate_report(
            LLMConfig("p"), "secret", "system", "raw",
            request_path=existing_file / "request.json",
        )
    assert existing_file.read_text() == "unchanged"


def test_extract_text_reports_empty_response():
    with pytest.raises(RuntimeError, match="SAFETY"):
        extract_text({"candidates": [{"finishReason": "SAFETY"}]})


def test_report_filename_is_filesystem_safe():
    assert report_filename("Alex O'Brien/Jr.") == "Alex-O-Brien-Jr.txt"


def test_http_errors_are_flagged_retryable_only_when_transient(monkeypatch):
    code = [503]

    def fake_urlopen(request, timeout):
        raise llm.urllib.error.HTTPError("u", code[0], "x", {}, io.BytesIO(b"busy"))

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(llm.LLMError, match="503") as busy:
        generate_report(LLMConfig("p"), "k", "s", "c")
    code[0] = 404
    with pytest.raises(llm.LLMError, match="404") as missing:
        generate_report(LLMConfig("p"), "k", "s", "c")

    assert busy.value.retryable and not missing.value.retryable


def test_parse_model_spec_and_labels():
    choice = llm.parse_model_spec("models/gemini-x/low")

    assert (choice.model, choice.level, choice.spec) == ("gemini-x", "low", "gemini-x/low")
    assert choice.label == "gemini-x / low thinking"
    assert llm.parse_model_spec("gemini-y").label == "gemini-y"
    with pytest.raises(ValueError):
        llm.parse_model_spec("/low")


def test_list_models_paginates_filters_and_expands_levels(monkeypatch):
    pages = {
        "": {"models": [
            {"name": "models/flash", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/embed", "supportedGenerationMethods": ["embedContent"]},
        ], "nextPageToken": "t2"},
        "t2": {"models": [
            {"name": "models/lite", "supportedGenerationMethods": ["generateContent"]},
        ]},
    }

    def fake_urlopen(request, timeout):
        token = request.full_url.partition("pageToken=")[2]
        assert request.get_header("X-goog-api-key") == "k"
        return io.BytesIO(json.dumps(pages[token]).encode())

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)

    choices = llm.list_models("k", {"flash": ("low", "high")})

    assert [c.spec for c in choices] == ["flash/low", "flash/high", "lite"]
    assert [c.spec for c in llm.list_models("k", {}, ("fl*",))] == ["lite"]


def test_generate_report_sends_thinking_level(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout):
        seen["body"] = json.loads(request.data)
        return io.BytesIO(json.dumps(
            {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}
        ).encode())

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)

    generate_report(LLMConfig("p", thinking_level="low"), "k", "s", "c")

    assert seen["body"]["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "low"}


def test_load_config_reads_thinking_levels(tmp_path):
    path = tmp_path / "llm.toml"
    path.write_text('prompt = "p"\nthinking_level = "low"\n[thinking_levels]\n"m" = ["low", "high"]\n')

    config = load_llm_config(path)

    assert config.thinking_level == "low" and config.thinking_levels == {"m": ("low", "high")}


def test_default_config_uses_packaged_then_user_override(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    packaged = load_llm_config()
    assert packaged.prompt and packaged.model

    (tmp_path / "cat-digest").mkdir()
    (tmp_path / "cat-digest" / "llm.toml").write_text('model = "custom"\n')
    merged = load_llm_config()

    assert merged.model == "custom"
    assert merged.prompt == packaged.prompt


def test_packaged_prompt_distinguishes_commit_activity_from_pr_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    prompt = load_llm_config().prompt
    assert "Whole-PR snapshot metrics" in prompt
    assert "only these dated commits" in prompt
    assert "not a net diff or a reliable" in prompt
