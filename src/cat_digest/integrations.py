from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
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
class GitHubCommit:
    sha: str
    authored_at: datetime
    message: str
    additions: int
    deletions: int


@dataclass(frozen=True)
class PullRequestMetrics:
    commits: int
    additions: int
    deletions: int
    period_commits: tuple[GitHubCommit, ...]


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
    pr_metrics: PullRequestMetrics | None = None


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


def _pull_request_metrics(
    pull: Any, expected_login: str, cutoff: datetime, end_date: datetime | None,
) -> PullRequestMetrics:
    commit_count = pull.commits
    if commit_count > 250:
        raise RuntimeError(
            f"Cannot collect complete commit activity for {pull.html_url}: "
            f"GitHub's pull request commits endpoint is limited to 250 commits "
            f"(PR reports {commit_count})"
        )
    commits = list(pull.get_commits())
    if len(commits) != commit_count:
        raise RuntimeError(
            f"Incomplete commit list for {pull.html_url}: "
            f"expected {commit_count}, received {len(commits)}; retry collection"
        )
    matching = []
    for commit in commits:
        if _login(commit.author).casefold() != expected_login:
            continue
        authored_at = commit.commit.author.date if commit.commit.author else None
        if authored_at is None:
            raise RuntimeError(f"Missing commit author date for {commit.sha} in {pull.html_url}")
        if not _within(authored_at, cutoff, end_date):
            continue
        stats = commit.stats
        if stats is None:
            raise RuntimeError(f"Missing commit statistics for {commit.sha} in {pull.html_url}")
        matching.append(GitHubCommit(
            sha=commit.sha,
            authored_at=authored_at,
            message=commit.commit.message or "",
            additions=stats.additions,
            deletions=stats.deletions,
        ))
    matching.sort(key=lambda commit: (commit.authored_at, commit.sha))
    return PullRequestMetrics(
        commits=commit_count,
        additions=pull.additions,
        deletions=pull.deletions,
        period_commits=tuple(matching),
    )


def collect_github(
    person: Person,
    token: str,
    cutoff: datetime,
    end_date: datetime | None = None,
    as_of: datetime | None = None,
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

    commit_end = end_date or as_of or datetime.now(timezone.utc)
    commit_filter = f"author-date:{cutoff_date}..{commit_end.date().isoformat()}"
    query = f"author:{person.github_user} {commit_filter}"
    LOG.debug("GitHub commit search: %s", query)
    LOG.debug(
        "GitHub commit-only PR discovery is best-effort: search covers default "
        "branches only and may return incomplete results"
    )
    commit_hits = 0
    discovered_pulls: dict[str, Any] = {}
    search_results = github.search_commits(query=query)
    incomplete_search = False
    for commit in search_results:
        incomplete_search |= bool(getattr(search_results, "incomplete_results", False))
        commit_hits += 1
        for pull in commit.get_pulls():
            if pull.html_url not in found:
                found[pull.html_url] = pull.as_issue()
                discovered_pulls[pull.html_url] = pull
        if commit_hits == 1000:
            break
    incomplete_search |= bool(getattr(search_results, "incomplete_results", False))
    if incomplete_search:
        LOG.warning("GitHub reported incomplete commit search results for %s", person.github_user)
    if commit_hits >= 1000:
        LOG.warning(
            "GitHub commit search reached the 1,000-result limit for %s; "
            "commit-only PR discovery may be incomplete", person.github_user,
        )
    LOG.debug("GitHub commit search returned %d result(s)", commit_hits)

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
        pr_metrics = None
        if is_pull_request:
            LOG.debug("GitHub: fetching pull request details and reviews for %s", issue.html_url)
            pull = discovered_pulls.get(issue.html_url)
            if pull is None:
                pull = issue.repository.get_pull(issue.number)
            status = (
                "merged"
                if pull.is_merged()
                else f"draft ({pull.state})" if pull.draft else pull.state
            )
            pr_metrics = _pull_request_metrics(pull, expected_login, cutoff, commit_end)
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
        authored_in_period = authored and _within(issue.created_at, cutoff, end_date)
        if not authored_in_period and not comments and not (pr_metrics and pr_metrics.period_commits):
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
            pr_metrics=pr_metrics,
        ))
    return sorted(entries, key=lambda item: item.created_at)
