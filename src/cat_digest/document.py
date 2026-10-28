from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from .core import Person
from .integrations import GitHubEntry


def _clean(value: Any) -> str:
    return " ".join(str(value).split())


def render_document(
    users: Mapping[str, Person],
    smartsheet: Mapping[str, Sequence[dict[str, Any]]],
    github: Mapping[str, Sequence[GitHubEntry]],
    generated_at: datetime,
    include_smartsheet: bool = True,
    include_github: bool = True,
    period: tuple[date, date] | None = None,
) -> str:
    lines = [f"CAT Digest | {generated_at.date().isoformat()}"]
    if period:
        lines.append(f"Period: {period[0].isoformat()} to {period[1].isoformat()}")
    lines.append("")
    github_names = {
        person.github_user.casefold(): person.name for person in users.values()
    }

    def display_github_user(login: str) -> str:
        return github_names.get(login.casefold(), login)

    for email, person in users.items():
        lines.extend([person.name, "=" * len(person.name), ""])
        if include_smartsheet:
            lines.extend(["SMARTSHEET REPORTS", ""])
            reports = smartsheet.get(email, [])
            if not reports:
                lines.extend(["No matching Smartsheet reports.", ""])
            for index, report in enumerate(reports, start=1):
                lines.append(f"{index}. {_clean(report['sheet'])}")
                lines.append(f"   Sheet: {report['url']}")
                for field, value in report["fields"].items():
                    lines.append(f"   {_clean(field)}: {_clean(value)}")
                lines.append("")

        if include_github:
            lines.extend(["GITHUB", ""])
            entries = github.get(email, [])
            if not entries:
                lines.extend(["No matching GitHub issues, pull requests, or comments.", ""])
            for index, entry in enumerate(entries, start=1):
                lines.extend([
                    f"{index}. {entry.kind}: {_clean(entry.title)}",
                    f"   {entry.url}",
                    f"   Author: {_clean(display_github_user(entry.author))}",
                ])
                assignees = ", ".join(
                    _clean(display_github_user(login))
                    for login in entry.assignees
                ) or "None"
                lines.append(f"   Assignees: {assignees}")
                if entry.kind == "Pull request" and entry.status:
                    lines.append(f"   Status: {_clean(entry.status)}")
                if entry.authored_in_period:
                    lines.append(f"   Created: {entry.created_at.date().isoformat()}")
                    label = "Details"
                else:
                    label = (
                        "Background context only, NOT work done in this period "
                        f"(item created {entry.created_at.date().isoformat()} by "
                        f"{_clean(display_github_user(entry.author))})"
                    )
                if entry.body.strip():
                    lines.append(f"   {label}: {_clean(entry.body)}")
                elif not entry.authored_in_period:
                    lines.append(f"   {label}")
                for comment in entry.comments:
                    lines.append(
                        f"   {comment.kind} ({comment.created_at.date().isoformat()}): "
                        f"{_clean(comment.body)}"
                    )
                lines.append("")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_document(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


PERIOD_RE = re.compile(r"^Period: (\d{4}-\d{2}-\d{2}) to (\d{4}-\d{2}-\d{2})$", re.M)


def parse_digest(text: str) -> tuple[dict[str, str], tuple[date, date] | None]:
    """Split a rendered digest into per-person sections and read its period, if any."""
    lines = text.splitlines()
    starts = [
        i for i in range(len(lines) - 1)
        if lines[i].strip() and lines[i + 1] == "=" * len(lines[i])
    ]
    sections = {}
    for n, start in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        sections[lines[start]] = "\n".join(lines[start:end]).rstrip() + "\n"
    match = PERIOD_RE.search(text)
    period = (
        (date.fromisoformat(match.group(1)), date.fromisoformat(match.group(2)))
        if match else None
    )
    return sections, period
