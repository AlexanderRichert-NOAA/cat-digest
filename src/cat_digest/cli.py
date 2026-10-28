from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from .core import Person, default_config_path, load_sheet_urls, load_users, write_config
from .document import parse_digest, render_document, write_document
from .integrations import (
    collect_github,
    collect_smartsheet,
)
from .llm import (
    LLMConfig,
    LLMError,
    apply_model_choice,
    build_model_choices,
    build_prompt,
    generate_report,
    list_models,
    load_llm_config,
    parse_model_spec,
    report_filename,
)


LOG = logging.getLogger("cat_digest")


def default_output_name(timestamp: datetime) -> str:
    return f"CAT-Digest-{timestamp:%Y-%m-%d-%H-%M}Z"


def parse_iso_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD format") from exc
    if parsed.isoformat() != value:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD format")
    return parsed


def _validate_questionary_date(value: str) -> bool | str:
    try:
        parse_iso_date(value)
    except argparse.ArgumentTypeError as exc:
        return str(exc)
    return True


def select_date_range(
    start_date: date | None,
    end_date: date | None,
    smartsheet_days: int,
    github_days: int,
    interactive: bool,
    today: date,
) -> tuple[date | None, date | None]:
    if (start_date is None) != (end_date is None):
        raise ValueError("--start-date and --end-date must be provided together")
    if start_date is not None and end_date is not None:
        if end_date < start_date:
            raise ValueError("--end-date must be on or after --start-date")
        return start_date, end_date
    if not interactive:
        return None, None

    import questionary

    use_custom_range = questionary.confirm(
        "Use a custom data range (default is last 30 days)?",
        default=False,
    ).ask()
    if use_custom_range is None:
        raise ValueError("date-range selection cancelled")
    if not use_custom_range:
        return None, None

    default_days = max(smartsheet_days, github_days)
    start_value = questionary.text(
        "Start date (YYYY-MM-DD):",
        default=(today - timedelta(days=default_days - 1)).isoformat(),
        validate=_validate_questionary_date,
    ).ask()
    end_value = questionary.text(
        "End date (YYYY-MM-DD):",
        default=today.isoformat(),
        validate=_validate_questionary_date,
    ).ask()
    if start_value is None or end_value is None:
        raise ValueError("date-range selection cancelled")
    selected_start = parse_iso_date(start_value)
    selected_end = parse_iso_date(end_value)
    if selected_end < selected_start:
        raise ValueError("end date must be on or after start date")
    return selected_start, selected_end


def select_users(
    users: dict[str, Person],
    selectors: list[str] | None = None,
) -> dict[str, Person]:
    if not selectors:
        return users
    identifiers: dict[str, set[str]] = {}
    for email, person in users.items():
        for identifier in (person.name, person.email, person.github_user):
            identifiers.setdefault(identifier.strip().casefold(), set()).add(email)
    selected: set[str] = set()
    for selector in selectors:
        matches = identifiers.get(selector.strip().casefold(), set())
        if not matches:
            raise ValueError(f"unknown user: {selector}")
        if len(matches) > 1:
            raise ValueError(f"ambiguous user name: {selector}; select by email or GitHub username")
        selected.update(matches)
    return {email: person for email, person in users.items() if email in selected}


def select_run_options(
    users: dict[str, Person],
    selectors: list[str] | None = None,
    all_users: bool = False,
    include_smartsheet: bool | None = None,
    include_github: bool | None = None,
    interactive: bool = False,
) -> tuple[dict[str, Person], bool, bool]:
    import questionary

    if selectors:
        selected_users = select_users(users, selectors)
    elif interactive and not all_users:
        answer = questionary.checkbox(
            "Which users should be included?",
            choices=[
                questionary.Choice(
                    f"{person.name} ({person.email})",
                    value=email,
                    checked=True,
                )
                for email, person in users.items()
            ],
        ).ask()
        if answer is None:
            raise ValueError("selection cancelled")
        selected_users = {email: users[email] for email in answer if email in users}
    else:
        selected_users = users
    if not selected_users:
        raise ValueError("select at least one user")

    if interactive and (include_smartsheet is None or include_github is None):
        source_choices = []
        if include_github is None:
            source_choices.append(
                questionary.Choice(
                    "GitHub issues, PRs, and comments",
                    value="github",
                    checked=True,
                )
            )
        if include_smartsheet is None:
            source_choices.append(
                questionary.Choice("Smartsheet reports", value="smartsheet", checked=False)
            )
        answer = questionary.checkbox(
            "Which sources should be included?",
            choices=source_choices,
        ).ask()
        if answer is None:
            raise ValueError("source selection cancelled")
        if include_smartsheet is None:
            include_smartsheet = "smartsheet" in answer
        if include_github is None:
            include_github = "github" in answer

    if include_smartsheet is None:
        include_smartsheet = True
    if include_github is None:
        include_github = True
    if not include_smartsheet and not include_github:
        raise ValueError("select at least one data source")

    return selected_users, include_smartsheet, include_github


def get_api_token(environment_variable: str) -> str:
    token = os.environ.get(environment_variable, "").strip()
    if token:
        return token
    if not sys.stdin.isatty():
        raise ValueError(
            f"{environment_variable} is not set; set it in the environment or run "
            "interactively to enter it securely"
        )

    import questionary

    guidance = {
        "SMARTSHEET_ACCESS_TOKEN": "Smartsheet access token with access to the configured sheets",
        "GEMINI_API_KEY": "Google AI Studio API key for generating per-person reports",
        "GITHUB_TOKEN": (
            "GitHub fine-grained PAT or App installation token with Issues: read and "
            "Pull requests: read on the target repositories, *or* public repo read access"
        ),
    }
    token = questionary.password(
        f"Enter {environment_variable}: {guidance.get(environment_variable, 'access token')} "
        "(input is hidden):"
    ).ask()
    if token is None or not token.strip():
        raise ValueError(f"no token provided for {environment_variable}")
    return token.strip()


def init_config() -> int:
    path = default_config_path()
    if path.exists() and input(f"{path} exists; overwrite? [y/N] ").strip().casefold() != "y":
        return 1
    name = input("Name: ").strip()
    email = input("Email: ").strip()
    github_user = input("GitHub handle: ").strip()
    print("(Optional) Smartsheet report URLs, one per line; blank line to finish:")
    urls = []
    while line := input("URL: ").strip():
        urls.append(line)
    try:
        write_config(path, [Person(name, email, github_user)], urls)
    except ValueError as exc:
        LOG.error("%s", exc)
        return 1
    print(f"Wrote {path}")
    return 0


def offer_init_config(args: argparse.Namespace) -> int | None:
    """Offer to create the config when it doesn't exist."""
    path = default_config_path()
    if path.exists() or (args.summarize_from and not args.user_selectors):
        return None
    if not sys.stdin.isatty():
        LOG.error("No config at %s; run `cat-digest --init-config`", path)
        return 1
    print(f"No config exists at {path}.")
    if input("Create the config now? [Y/n] ").strip().casefold() in ("", "y", "yes"):
        return init_config() or None
    return 1


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Consolidate recent Smartsheet and GitHub contributions into a text digest."
    )
    parser.add_argument(
        "--init-config",
        action="store_true",
        help=f"Interactively create the config at {default_config_path()} and exit.",
    )
    parser.add_argument(
        "--user",
        dest="user_selectors",
        action="append",
        metavar="NAME|EMAIL|GITHUB_USER",
        help="Select a configured user; repeat for multiple users.",
    )
    parser.add_argument(
        "--all-users",
        action="store_true",
        help="Use every configured user without prompting for contributors.",
    )
    parser.add_argument(
        "--smartsheet",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Include Smartsheet reports; use --no-smartsheet to omit them.",
    )
    parser.add_argument(
        "--github",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Include GitHub activity; use --no-github to omit all GitHub content.",
    )
    parser.add_argument("--smartsheet-days", type=int, default=30)
    parser.add_argument("--github-days", type=int, default=30)
    parser.add_argument(
        "--start-date",
        type=parse_iso_date,
        help="Inclusive start date (YYYY-MM-DD); requires --end-date.",
    )
    parser.add_argument(
        "--end-date",
        type=parse_iso_date,
        help="Inclusive end date (YYYY-MM-DD); requires --start-date.",
    )
    parser.add_argument(
        "--smartsheet-date-column",
        help="Exact date column title to use across sheets; otherwise detected from column titles.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("outputs"),
        help="Root directory under which CAT-Digest-YYYY-MM-DD-HH-MMZ output directories are created (default: outputs/).",
    )
    parser.add_argument(
        "--llm",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Generate per-person LLM reports with Google AI Studio; use --no-llm to skip.",
    )
    parser.add_argument(
        "--llm-config",
        type=Path,
        default=None,
        help="TOML file with the LLM prompt and model settings "
        "(default: ~/.config/cat-digest/llm.toml over the packaged defaults).",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Show individual Smartsheet and GitHub requests and other detailed progress.",
    )
    parser.add_argument(
        "--model",
        metavar="MODEL[/THINKING_LEVEL]",
        help="Gemini model and optional thinking level, e.g. gemini-3.8-flash/low; skips the model prompt.",
    )
    parser.add_argument(
        "--summarize-from",
        type=Path,
        metavar="DIGEST_OR_DIR",
        help="Skip collection; generate missing LLM reports from an existing raw digest file or its directory.",
    )
    return parser


def select_model(
    config: LLMConfig,
    api_key: str,
    spec: str | None,
    interactive: bool,
) -> LLMConfig:
    if spec:
        return apply_model_choice(config, parse_model_spec(spec))
    if not interactive:
        raise ValueError("no terminal available to choose a model; use --model MODEL[/THINKING_LEVEL]")

    try:
        choices = list_models(api_key, config.thinking_levels, config.exclude_models)
    except LLMError as exc:
        LOG.warning("%s; offering models from %s only", exc, "the LLM config")
        choices = build_model_choices([config.model], config.thinking_levels)
    if not choices:
        raise ValueError("the Gemini API reported no models that support generateContent")

    import questionary

    default = parse_model_spec(
        f"{config.model}/{config.thinking_level}" if config.thinking_level else config.model
    )
    if default not in choices:
        default = next((c for c in choices if c.model == config.model), choices[0])
    answer = questionary.select(
        "Which Gemini model should generate the reports?",
        choices=[
            questionary.Choice(
                f"{c.label} (default)" if c == default else c.label, value=c
            )
            for c in choices
        ],
        default=default,
        # questionary styles the `default` choice as "selected" permanently; plain
        # text keeps the cursor-start behaviour without the sticky highlight.
        style=questionary.Style([("selected", "noreverse nobold")]),
    ).ask()
    if answer is None:
        raise ValueError("model selection cancelled")
    return apply_model_choice(config, answer)


def generate_reports(
    sections: dict[str, str],
    period: tuple[date, date],
    config: LLMConfig,
    api_key: str,
    output_dir: Path,
    interactive: bool,
    digest_path: Path,
) -> int:
    for name, raw in sections.items():
        while True:
            LOG.info("Generating LLM report for %s", name)
            try:
                report = generate_report(
                    config,
                    api_key,
                    build_prompt(config, name, period[0].isoformat(), period[1].isoformat()),
                    raw,
                )
                write_document(output_dir / report_filename(name), report)
                print(f"\n===== {name} =====\n{report.rstrip()}\n")
                break
            except Exception as exc:
                LOG.error("Could not generate LLM report for %s: %s", name, exc)
                if interactive and getattr(exc, "retryable", False):
                    import questionary

                    choice = questionary.select(
                        "The Gemini API is temporarily unavailable. Retry now?",
                        choices=[
                            questionary.Choice("Retry", value="retry"),
                            questionary.Choice("Exit", value="exit"),
                        ],
                    ).ask()
                    if choice == "retry":
                        continue
                print(
                    "Stopped generating LLM reports. The raw digest is saved; to generate "
                    f"the remaining reports later, run: cat-digest --summarize-from {digest_path}"
                )
                return 1

    print(f"Saved reports to {output_dir}")
    return 0


def summarize_existing(args: argparse.Namespace, now: datetime) -> int:
    path = args.summarize_from
    digest_path = path
    if path.is_dir():
        candidates = sorted(path.glob("CAT-Digest-*.txt"))
        if not candidates:
            LOG.error("No CAT-Digest-*.txt file found in %s", path)
            return 1
        digest_path = candidates[-1]
    try:
        sections, period = parse_digest(digest_path.read_text(encoding="utf-8"))
        config = load_llm_config(args.llm_config)
        if args.user_selectors:
            wanted = {p.name for p in select_users(load_users(), args.user_selectors).values()}
            sections = {name: raw for name, raw in sections.items() if name in wanted}
        if args.start_date and args.end_date:
            period = (args.start_date, args.end_date)
        elif period is None:
            LOG.error("%s has no period; provide --start-date and --end-date", digest_path)
            return 1
        output_dir = digest_path.parent
        sections = {
            name: raw for name, raw in sections.items()
            if not (output_dir / report_filename(name)).exists()
        }
        if not sections:
            print("All reports already exist; nothing to do.")
            return 0
        api_key = get_api_token("GEMINI_API_KEY")
        config = select_model(config, api_key, args.model, sys.stdin.isatty())
    except (OSError, ValueError, LLMError) as exc:
        LOG.error("%s", exc)
        return 1
    return generate_reports(
        sections, period, config, api_key, output_dir,
        sys.stdin.isatty(), digest_path,
    )


class TruncateThirdPartyFilter(logging.Filter):
    """Keep only the first and last `limit` characters of long library log messages."""

    def __init__(self, limit: int = 150):
        super().__init__()
        self.limit = limit

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name.split(".")[0] == "cat_digest":
            return True
        message = record.getMessage()
        if len(message) > 2 * self.limit:
            omitted = len(message) - 2 * self.limit
            message = f"{message[:self.limit]} ... [{omitted} chars omitted] ... {message[-self.limit:]}"
        record.msg, record.args = message, ()
        return True


def configure_logging(debug: bool) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    # The Smartsheet SDK logs every request at INFO, so keep it quiet unless debugging.
    logging.getLogger("smartsheet").setLevel(logging.DEBUG if debug else logging.WARNING)
    if debug:
        for handler in logging.getLogger().handlers:
            if not any(isinstance(f, TruncateThirdPartyFilter) for f in handler.filters):
                handler.addFilter(TruncateThirdPartyFilter())
        for name in ("cat_digest", "urllib3.connectionpool"):
            logging.getLogger(name).setLevel(logging.DEBUG)


def main(argv: list[str] | None = None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)
    configure_logging(args.debug)
    if args.init_config:
        return init_config()
    if (code := offer_init_config(args)) is not None:
        return code
    if args.smartsheet_days < 1 or args.github_days < 1:
        parser.error("lookback day counts must be at least 1")
    if args.user_selectors and args.all_users:
        parser.error("--user and --all-users cannot be used together")
    now = datetime.now(timezone.utc)
    if args.summarize_from:
        return summarize_existing(args, now)

    try:
        configured_users = load_users()
        users, include_smartsheet, include_github = select_run_options(
            configured_users,
            selectors=args.user_selectors,
            all_users=args.all_users,
            include_smartsheet=args.smartsheet,
            include_github=args.github,
            interactive=sys.stdin.isatty(),
        )
        start_date, end_date = select_date_range(
            args.start_date,
            args.end_date,
            args.smartsheet_days,
            args.github_days,
            sys.stdin.isatty(),
            now.date(),
        )
        sheets = load_sheet_urls() if include_smartsheet else []
        llm_config = load_llm_config(args.llm_config) if args.llm else None
        gemini_key = get_api_token("GEMINI_API_KEY") if args.llm else None
        if llm_config is not None:
            llm_config = select_model(llm_config, gemini_key, args.model, sys.stdin.isatty())
    except (OSError, ValueError, LLMError) as exc:
        parser.error(str(exc))

    try:
        smartsheet_data = {email: [] for email in users}
        github_data = {email: [] for email in users}
        if start_date is not None and end_date is not None:
            sheet_cutoff = datetime.combine(start_date, time.min, timezone.utc)
            github_cutoff = sheet_cutoff
            range_end = datetime.combine(end_date, time.max, timezone.utc)
        else:
            sheet_cutoff = now - timedelta(days=args.smartsheet_days)
            github_cutoff = now - timedelta(days=args.github_days)
            range_end = None

        if include_smartsheet:
            smartsheet_token = get_api_token("SMARTSHEET_ACCESS_TOKEN")
            LOG.info("Reading %d Smartsheet sheet(s)", len(sheets))
            for ref in sheets:
                LOG.debug("Reading Smartsheet %s (%s)", ref.sheet_id, ref.api_base)
                collected = collect_smartsheet(
                    ref,
                    users,
                    sheet_cutoff,
                    smartsheet_token,
                    date_column=args.smartsheet_date_column,
                    end_date=range_end,
                )
                for email, reports in collected.items():
                    smartsheet_data[email].extend(reports)
            LOG.info(
                "Found %d Smartsheet report(s)",
                sum(len(reports) for reports in smartsheet_data.values()),
            )

        if include_github:
            github_token = get_api_token("GITHUB_TOKEN")
            LOG.info("Reading GitHub activity for %d user(s)", len(users))
            for email, person in users.items():
                LOG.debug("Reading GitHub activity for %s", person.github_user)
                github_data[email] = collect_github(
                    person,
                    github_token,
                    github_cutoff,
                    end_date=range_end,
                )
            LOG.info(
                "Found %d GitHub item(s)",
                sum(len(entries) for entries in github_data.values()),
            )

        period = (
            start_date or (now - timedelta(days=max(args.smartsheet_days, args.github_days))).date(),
            end_date or now.date(),
        )
        content = render_document(
            users,
            smartsheet_data,
            github_data,
            now,
            include_smartsheet=include_smartsheet,
            include_github=include_github,
            period=period,
        )
        output_name = default_output_name(now)
        output_dir = args.output_dir / output_name
        output_path = write_document(output_dir / f"{output_name}.txt", content)
    except Exception:
        LOG.exception("Could not collect contributions or save the digest")
        return 1

    print(f"Saved digest to {output_path}")
    if llm_config is None:
        return 0

    sections = {}
    for email, person in users.items():
        if not smartsheet_data[email] and not github_data[email]:
            LOG.info("No activity for %s; skipping LLM report", person.name)
            continue
        sections[person.name] = render_document(
            {email: person},
            smartsheet_data,
            github_data,
            now,
            include_smartsheet=include_smartsheet,
            include_github=include_github,
        )
    return generate_reports(
        sections, period, llm_config, gemini_key, output_dir,
        sys.stdin.isatty(), output_path,
    )


if __name__ == "__main__":
    sys.exit(main())
