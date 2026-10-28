from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from .core import Person, SheetRef, filter_smartsheet_rows

LOG = logging.getLogger("cat_digest")


@dataclass(frozen=True)
class GitHubComment:
    author: str
    created_at: datetime
    body: str
    kind: str


@dataclass(frozen=True)
class GitHubEntry:
    kind: str
    title: str
    url: str
    created_at: datetime
    body: str
    author: str
    assignees: tuple[str, ...]
    comments: tuple[GitHubComment, ...]
    status: str | None = None
    authored_in_period: bool = True


def _fetch_sheet(client: Any, ref: SheetRef, page_size: int = 10_000) -> tuple[Any, list[Any]]:
    page = 1
    rows: list[Any] = []
    first_sheet = None
    while True:
        sheet = client.Sheets.get_sheet(
            ref.sheet_id,
            include="objectValue",
            page_size=page_size,
            page=page,
        )
        if first_sheet is None:
            first_sheet = sheet
        page_rows = list(sheet.rows or [])
        LOG.debug("Smartsheet %s page %d: %d row(s)", ref.sheet_id, page, len(page_rows))
        rows.extend(page_rows)
        if len(page_rows) < page_size:
            return first_sheet, rows
        page += 1


def collect_smartsheet(
    ref: SheetRef,
    users: Mapping[str, Person],
    cutoff: datetime,
    token: str,
    date_column: str | None = None,
    end_date: datetime | None = None,
) -> dict[str, list[dict[str, Any]]]:
    import smartsheet

    client = smartsheet.Smartsheet(token, api_base=ref.api_base)
    client.errors_as_exceptions(True)
    sheet, rows = _fetch_sheet(client, ref)
    records = filter_smartsheet_rows(
        rows,
        sheet.columns,
        users,
        cutoff,
        date_column=date_column,
        end_date=end_date,
    )
    result: dict[str, list[dict[str, Any]]] = {email: [] for email in users}
    LOG.debug(
        "Smartsheet %r: %d row(s) fetched, %d matched",
        sheet.name, len(rows), sum(len(matched) for matched in records.values()),
    )
    for email, rows in records.items():
        result[email].extend(
            {"sheet": sheet.name, "url": ref.url, "fields": row} for row in rows
        )
    return result


def _login(user: Any) -> str:
    return str(getattr(user, "login", ""))


def _within(
    value: datetime | None,
    cutoff: datetime,
    end_date: datetime | None = None,
) -> bool:
    if value is None:
        return False
    normalized = value.replace(tzinfo=value.tzinfo or cutoff.tzinfo)
    return normalized >= cutoff and (end_date is None or normalized <= end_date)


def collect_github(
    person: Person,
    token: str,
    cutoff: datetime,
    end_date: datetime | None = None,
) -> list[GitHubEntry]:
    from github import Auth, Github

    github = Github(auth=Auth.Token(token))
    found: dict[str, Any] = {}
    cutoff_date = cutoff.date().isoformat()
    updated_filter = (
        f"updated:{cutoff_date}..{end_date.date().isoformat()}"
        if end_date is not None
        else f"updated:>={cutoff_date}"
    )
    for item_type in ("issue", "pull-request"):
        for activity in ("author", "commenter"):
            query = (
                f"is:{item_type} {activity}:{person.github_user} "
                f"{updated_filter}"
            )
            LOG.debug("GitHub search: %s", query)
            hits = 0
            for issue in github.search_issues(query=query):
                found[issue.html_url] = issue
                hits += 1
            LOG.debug("GitHub search returned %d result(s)", hits)

    entries: list[GitHubEntry] = []
    expected_login = person.github_user.casefold()
    LOG.debug("GitHub: %d unique item(s) to inspect for %s", len(found), person.github_user)
    for issue in found.values():
        LOG.debug("GitHub: fetching comments for %s", issue.html_url)
        authored = _login(issue.user).casefold() == expected_login
        comments: list[GitHubComment] = []
        for comment in issue.get_comments():
            if (
                _login(comment.user).casefold() == expected_login
                and _within(comment.created_at, cutoff, end_date)
            ):
                comments.append(GitHubComment(
                    author=person.github_user,
                    created_at=comment.created_at,
                    body=comment.body or "",
                    kind="Comment",
                ))

        is_pull_request = getattr(issue, "pull_request", None) is not None
        status = None
        if is_pull_request:
            LOG.debug("GitHub: fetching pull request details and reviews for %s", issue.html_url)
            pull = issue.repository.get_pull(issue.number)
            status = (
                "merged"
                if pull.is_merged()
                else f"draft ({pull.state})" if pull.draft else pull.state
            )
            for comment in pull.get_review_comments():
                if (
                    _login(comment.user).casefold() == expected_login
                    and _within(comment.created_at, cutoff, end_date)
                ):
                    comments.append(GitHubComment(
                        author=person.github_user,
                        created_at=comment.created_at,
                        body=comment.body or "",
                        kind="Review comment",
                    ))
            for review in pull.get_reviews():
                submitted_at = getattr(review, "submitted_at", None)
                if (
                    _login(review.user).casefold() == expected_login
                    and _within(submitted_at, cutoff, end_date)
                    and (review.body or "").strip()
                ):
                    comments.append(GitHubComment(
                        author=person.github_user,
                        created_at=submitted_at,
                        body=review.body,
                        kind="Review",
                    ))

        comments.sort(key=lambda item: item.created_at)
        # Activity by other people (which bumps updated_at) is not this person's work, so an
        # authored item counts only if it was created in the window; otherwise it needs
        # in-window comments or reviews and its body is background context only.
        authored_in_period = authored and _within(issue.created_at, cutoff, end_date)
        if not authored_in_period and not comments:
            continue
        entries.append(GitHubEntry(
            kind="Pull request" if is_pull_request else "Issue",
            title=issue.title,
            url=issue.html_url,
            created_at=issue.created_at,
            body=issue.body or "",
            author=_login(issue.user),
            assignees=tuple(
                sorted({
                    login
                    for assignee in getattr(issue, "assignees", []) or []
                    if (login := _login(assignee))
                })
            ),
            comments=tuple(comments),
            status=status,
            authored_in_period=authored_in_period,
        ))
    return sorted(entries, key=lambda item: item.created_at)


