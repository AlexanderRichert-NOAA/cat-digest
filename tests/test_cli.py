import pytest
import questionary
from types import SimpleNamespace
from datetime import date, datetime, timezone

from cat_digest.cli import (
    default_output_name,
    get_api_token,
    parse_iso_date,
    select_date_range,
    select_run_options,
    select_users,
)
from cat_digest.core import Person


@pytest.fixture
def users():
    return {
        "one@example.com": Person("First Person", "one@example.com", "first-user"),
        "two@example.com": Person("Second Person", "two@example.com", "second-user"),
    }


def test_select_users_accepts_name_email_and_github_username(users):
    selected = select_users(users, ["second-user", "one@example.com"])

    assert list(selected) == ["one@example.com", "two@example.com"]


def test_select_users_rejects_unknown_selector(users):
    with pytest.raises(ValueError, match="unknown user"):
        select_users(users, ["missing-user"])


def test_interactive_prompt_selects_users_and_sources(users, monkeypatch):
    prompts = []
    answers = iter([
        ["two@example.com"],
        ["github"],
    ])

    def checkbox(message, choices):
        prompts.append((message, choices))
        return SimpleNamespace(ask=lambda: next(answers))

    monkeypatch.setattr(questionary, "checkbox", checkbox)
    selected, include_smartsheet, include_github = select_run_options(
        users,
        interactive=True,
    )

    assert list(selected) == ["two@example.com"]
    assert not include_smartsheet
    assert include_github
    assert [prompt[0] for prompt in prompts] == [
        "Which users should be included?",
        "Which sources should be included?",
    ]
    assert all(choice.checked for choice in prompts[0][1])
    assert {choice.value: choice.checked for choice in prompts[1][1]} == {
        "smartsheet": False,
        "github": True,
    }


def test_noninteractive_selection_defaults_to_all(users):
    assert select_run_options(users) == (users, True, True)


def test_default_output_filename_includes_utc_hour_and_minute():
    timestamp = datetime(2026, 10, 15, 14, 7, tzinfo=timezone.utc)

    assert default_output_name(timestamp) == "CAT-Digest-2026-10-15-14-07Z"


def test_cli_date_range_is_inclusive_and_skips_prompt(monkeypatch):
    monkeypatch.setattr(
        questionary,
        "confirm",
        lambda *args, **kwargs: pytest.fail("unexpected date-range prompt"),
    )

    selected = select_date_range(
        parse_iso_date("2026-09-01"),
        parse_iso_date("2026-09-30"),
        30,
        30,
        True,
        date(2026, 10, 15),
    )

    assert selected == (date(2026, 9, 1), date(2026, 9, 30))


def test_questionary_can_select_custom_inclusive_date_range(monkeypatch):
    prompts = []
    text_values = iter(["2026-09-01", "2026-09-30"])

    def confirm(message, default):
        prompts.append(("confirm", message, default))
        return SimpleNamespace(ask=lambda: True)

    def text(message, default, validate):
        prompts.append(("text", message, default, validate))
        return SimpleNamespace(ask=lambda: next(text_values))

    monkeypatch.setattr(questionary, "confirm", confirm)
    monkeypatch.setattr(questionary, "text", text)

    selected = select_date_range(
        None,
        None,
        30,
        30,
        True,
        date(2026, 10, 15),
    )

    assert selected == (date(2026, 9, 1), date(2026, 9, 30))
    assert prompts[0][2] is False
    assert prompts[1][3]("2026-09-01") is True
    assert prompts[1][3]("not-a-date")


def test_date_range_requires_both_dates_and_order():
    with pytest.raises(ValueError, match="provided together"):
        select_date_range(date(2026, 9, 1), None, 30, 30, False, date(2026, 10, 15))
    with pytest.raises(ValueError, match="on or after"):
        select_date_range(date(2026, 9, 2), date(2026, 9, 1), 30, 30, False, date(2026, 10, 15))


def test_get_api_token_uses_environment_without_prompt(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "  from-env  ")
    monkeypatch.setattr(
        questionary,
        "password",
        lambda *args, **kwargs: pytest.fail("unexpected password prompt"),
    )

    assert get_api_token("GITHUB_TOKEN") == "from-env"


def test_get_api_token_prompts_with_hidden_input(monkeypatch):
    prompt = {}
    monkeypatch.delenv("SMARTSHEET_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(
        "cat_digest.cli.sys.stdin",
        SimpleNamespace(isatty=lambda: True),
    )
    def password(message):
        prompt["message"] = message
        return SimpleNamespace(ask=lambda: "entered-token")

    monkeypatch.setattr(questionary, "password", password)

    assert get_api_token("SMARTSHEET_ACCESS_TOKEN") == "entered-token"
    assert "hidden" in prompt["message"]


def test_github_token_prompt_describes_required_permissions(monkeypatch):
    prompt = {}
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(
        "cat_digest.cli.sys.stdin",
        SimpleNamespace(isatty=lambda: True),
    )

    def password(message):
        prompt["message"] = message
        return SimpleNamespace(ask=lambda: "github-token")

    monkeypatch.setattr(questionary, "password", password)

    assert get_api_token("GITHUB_TOKEN") == "github-token"
    assert "fine-grained PAT or App installation token" in prompt["message"]
    assert "Issues: read" in prompt["message"]
    assert "Pull requests: read" in prompt["message"]


def test_get_api_token_explains_noninteractive_environment_requirement(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(
        "cat_digest.cli.sys.stdin",
        SimpleNamespace(isatty=lambda: False),
    )

    with pytest.raises(ValueError, match="GITHUB_TOKEN is not set"):
        get_api_token("GITHUB_TOKEN")


def test_explicit_options_skip_interactive_prompt(users, monkeypatch):
    monkeypatch.setattr(
        questionary,
        "checkbox",
        lambda *args, **kwargs: pytest.fail("unexpected prompt"),
    )
    selected = select_run_options(
        users,
        selectors=["first-user"],
        include_smartsheet=False,
        include_github=True,
        interactive=True,
    )

    assert selected == ({"one@example.com": users["one@example.com"]}, False, True)


def test_selection_requires_user_and_source(users, monkeypatch):
    monkeypatch.setattr(
        questionary,
        "checkbox",
        lambda *args, **kwargs: SimpleNamespace(ask=lambda: []),
    )

    with pytest.raises(ValueError, match="at least one user"):
        select_run_options(users, interactive=True)

    with pytest.raises(ValueError, match="at least one data source"):
        select_run_options(users, include_smartsheet=False, include_github=False)

def test_summarize_from_generates_only_missing_reports(tmp_path, monkeypatch):
    from cat_digest import cli

    (tmp_path / "CAT-Digest-x.txt").write_text(
        "CAT Digest | 2026-10-15\nPeriod: 2026-09-15 to 2026-10-15\n\n"
        "Ann One\n=======\n\nraw a\n\nBob Two\n=======\n\nraw b\n"
    )
    (tmp_path / "Ann-One.txt").write_text("done\n")
    (tmp_path / "llm.toml").write_text('prompt = "Summarize {name}"\n')
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    seen = []
    monkeypatch.setattr(
        cli, "generate_report",
        lambda config, key, prompt, raw: seen.append((prompt, raw, config.model, config.thinking_level)) or "summary\n",
    )

    status = cli.main(["--summarize-from", str(tmp_path), "--llm-config", str(tmp_path / "llm.toml"), "--model", "m/low"])

    assert status == 0
    assert seen == [("Summarize Bob Two", "Bob Two\n=======\n\nraw b\n", "m", "low")]
    assert (tmp_path / "Bob-Two.txt").read_text() == "summary\n"


def test_select_model_requires_flag_without_terminal():
    from cat_digest.cli import select_model
    from cat_digest.llm import LLMConfig

    config = LLMConfig("p")

    assert select_model(config, "k", "m/high", False).thinking_level == "high"
    with pytest.raises(ValueError, match="--model"):
        select_model(config, "k", None, False)


def test_select_model_prompts_with_live_choices(monkeypatch):
    from cat_digest import cli
    from cat_digest.llm import LLMConfig, ModelChoice

    monkeypatch.setattr(
        cli, "list_models", lambda key, levels, exclude: [ModelChoice("m", "low"), ModelChoice("m", "high")]
    )
    shown = {}

    def fake_select(message, choices, default, style=None):
        shown["labels"] = [c.title for c in choices]
        shown["default"] = default
        return SimpleNamespace(ask=lambda: choices[1].value)

    monkeypatch.setattr(questionary, "select", fake_select)

    result = cli.select_model(LLMConfig("p", "m", thinking_level="low"), "k", None, True)

    assert shown["labels"] == ["m / low thinking (default)", "m / high thinking"]
    assert shown["default"] == ModelChoice("m", "low")
    assert (result.model, result.thinking_level) == ("m", "high")


def test_model_flag_without_level_leaves_thinking_to_api_default():
    from cat_digest.cli import select_model
    from cat_digest.llm import LLMConfig

    config = LLMConfig("p", "other", thinking_level="low")

    result = select_model(config, "k", "gemini-3.8-flash", False)

    assert (result.model, result.thinking_level) == ("gemini-3.8-flash", None)


def test_report_failure_offers_manual_retry_then_exits(tmp_path, monkeypatch, capsys):
    from cat_digest import cli
    from cat_digest.llm import LLMConfig, LLMError

    attempts = []

    def flaky(config, key, prompt, raw):
        attempts.append(1)
        if len(attempts) < 3:
            raise LLMError("503 busy", retryable=True)
        return "summary\n"

    answers = iter(["retry", "retry"])
    monkeypatch.setattr(cli, "generate_report", flaky)
    monkeypatch.setattr(
        questionary, "select", lambda *a, **k: SimpleNamespace(ask=lambda: next(answers))
    )
    sections = {"Ann One": "raw"}
    period = (date(2026, 9, 1), date(2026, 9, 30))

    assert cli.generate_reports(sections, period, LLMConfig("p"), "k", tmp_path, True, tmp_path / "d.txt") == 0
    assert (tmp_path / "Ann-One.txt").exists() and len(attempts) == 3

    attempts.clear()
    answers = iter(["exit"])
    monkeypatch.setattr(cli, "generate_report", lambda *a: (_ for _ in ()).throw(LLMError("503", True)))
    assert cli.generate_reports(sections, period, LLMConfig("p"), "k", tmp_path, True, tmp_path / "d.txt") == 1
    assert "--summarize-from" in capsys.readouterr().out


def test_debug_flag_enables_debug_logging_for_selected_loggers():
    import logging

    from cat_digest.cli import configure_logging

    try:
        configure_logging(False)
        assert not logging.getLogger("cat_digest").isEnabledFor(logging.DEBUG)
        assert not logging.getLogger("smartsheet.smartsheet").isEnabledFor(logging.INFO)
        configure_logging(True)
        assert logging.getLogger("cat_digest").isEnabledFor(logging.DEBUG)
        assert logging.getLogger("smartsheet.smartsheet").isEnabledFor(logging.DEBUG)
        assert not logging.getLogger("github").isEnabledFor(logging.DEBUG)
    finally:
        for name in ("cat_digest", "urllib3.connectionpool", "smartsheet"):
            logging.getLogger(name).setLevel(logging.NOTSET)


def test_third_party_log_messages_are_truncated_but_ours_are_not():
    import logging

    from cat_digest.cli import TruncateThirdPartyFilter

    def record(name):
        return logging.LogRecord(name, logging.DEBUG, "f", 1, "x" * 1000, None, None)

    theirs, ours = record("smartsheet.smartsheet"), record("cat_digest")
    log_filter = TruncateThirdPartyFilter(150)

    assert log_filter.filter(theirs) and log_filter.filter(ours)
    assert theirs.getMessage().startswith("x" * 150 + " ... [700 chars omitted]")
    assert theirs.getMessage().endswith("x" * 150)
    assert len(ours.getMessage()) == 1000
