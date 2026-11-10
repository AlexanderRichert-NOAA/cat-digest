from datetime import datetime, timezone
import sys
from types import SimpleNamespace

import pytest

from cat_digest.core import (
    Person,
    filter_smartsheet_rows,
    default_config_path,
    load_config,
    load_sheet_urls,
    load_users,
    write_config,
    parse_datetime,
    parse_sheet_url,
)
from cat_digest.integrations import _fetch_sheet, _pull_request_metrics, collect_github
from cat_digest.core import SheetRef


def test_load_config_files(tmp_path):
    cfg = tmp_path / "config.yaml"
    write_config(
        cfg,
        [Person("A Person", "a@example.com", "aperson"), Person("B", "b@example.com", "b")],
        ["https://app.smartsheetgov.com/sheets/AbC123"],
    )

    users, refs = load_config(cfg)

    assert users["a@example.com"] == Person("A Person", "a@example.com", "aperson")
    assert list(users) == ["a@example.com", "b@example.com"]
    assert refs[0].sheet_id == "AbC123"
    assert refs[0].api_base == "https://api.smartsheetgov.com/2.0"


def test_config_validation(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("users:\n  - name: A\n    email: a@example.com\n")
    with pytest.raises(ValueError, match="missing: github_user"):
        load_config(cfg)
    cfg.write_text("users: []\n")
    with pytest.raises(ValueError, match="non-empty list"):
        load_config(cfg)
    with pytest.raises(ValueError, match="supported Smartsheet"):
        parse_sheet_url("https://example.com/sheets/123")


def test_filter_smartsheet_rows_by_date_and_contact():
    person = Person("A Person", "a@example.com", "aperson")
    columns = [
        SimpleNamespace(id=1, title="Submitted By"),
        SimpleNamespace(id=2, title="Date Submitted"),
        SimpleNamespace(id=3, title="Task Name"),
        SimpleNamespace(id=4, title="Progress Report"),
    ]
    recent = SimpleNamespace(cells=[
        SimpleNamespace(column_id=1, value="A Person"),
        SimpleNamespace(column_id=2, value="2026-10-01"),
        SimpleNamespace(column_id=3, value="Task A"),
        SimpleNamespace(column_id=4, value="Shipped work"),
    ])
    start_boundary = SimpleNamespace(cells=[
        SimpleNamespace(column_id=1, value="A Person"),
        SimpleNamespace(column_id=2, value="2026-09-15"),
        SimpleNamespace(column_id=3, value="Task at start"),
        SimpleNamespace(column_id=4, value="Included at start"),
    ])
    old = SimpleNamespace(cells=[
        SimpleNamespace(column_id=1, value="A Person"),
        SimpleNamespace(column_id=2, value="2026-08-01"),
        SimpleNamespace(column_id=3, value="Task B"),
        SimpleNamespace(column_id=4, value="Old work"),
    ])
    after_range = SimpleNamespace(cells=[
        SimpleNamespace(column_id=1, value="A Person"),
        SimpleNamespace(column_id=2, value="2026-10-02"),
        SimpleNamespace(column_id=3, value="Task C"),
        SimpleNamespace(column_id=4, value="After range"),
    ])

    records = filter_smartsheet_rows(
        [start_boundary, recent, old, after_range], columns, {"a@example.com": person},
        datetime(2026, 9, 15, tzinfo=timezone.utc),
        end_date=datetime(2026, 10, 1, 23, 59, 59, tzinfo=timezone.utc),
    )

    assert records["a@example.com"] == [
        {
            "Submitted By": "A Person",
            "Date Submitted": "2026-09-15",
            "Task Name": "Task at start",
            "Progress Report": "Included at start",
        },
        {
            "Submitted By": "A Person",
            "Date Submitted": "2026-10-01",
            "Task Name": "Task A",
            "Progress Report": "Shipped work",
        },
    ]


def test_parse_datetime_normalizes_utc():
    assert parse_datetime("2026-10-01T12:00:00-04:00") == datetime(
        2026, 10, 1, 16, tzinfo=timezone.utc
    )


def test_collect_github_keeps_recent_user_comments_and_reviews(monkeypatch, caplog):
    cutoff = datetime(2026, 9, 15, tzinfo=timezone.utc)
    recent = datetime(2026, 10, 1, tzinfo=timezone.utc)
    stale = datetime(2026, 8, 1, tzinfo=timezone.utc)
    user = SimpleNamespace(login="aperson")
    other = SimpleNamespace(login="someone-else")
    issue_comment = SimpleNamespace(user=user, created_at=recent, body="Recent comment")
    old_comment = SimpleNamespace(user=user, created_at=stale, body="Old comment")
    after_range_comment = SimpleNamespace(
        user=user,
        created_at=datetime(2026, 10, 2, tzinfo=timezone.utc),
        body="After range",
    )
    start_boundary_comment = SimpleNamespace(
        user=user,
        created_at=cutoff,
        body="At start",
    )
    review_comment = SimpleNamespace(user=user, created_at=recent, body="Review note")
    pull = SimpleNamespace(
        is_merged=lambda: False,
        state="open",
        draft=False,
        html_url="https://github.com/example/repo/pull/2",
        commits=0,
        additions=0,
        deletions=0,
        get_commits=lambda: [],
        get_review_comments=lambda: [review_comment],
        get_reviews=lambda: [],
    )
    merged_pull = SimpleNamespace(
        is_merged=lambda: True,
        state="closed",
        draft=False,
        html_url="https://github.com/example/repo/pull/3",
        commits=0,
        additions=0,
        deletions=0,
        get_commits=lambda: [],
        get_review_comments=lambda: [],
        get_reviews=lambda: [],
    )
    repository = SimpleNamespace(get_pull=lambda number: merged_pull if number == 3 else pull)
    authored_issue = SimpleNamespace(
        user=user,
        assignees=[user, other],
        title="Current issue",
        html_url="https://github.com/example/repo/issues/1",
        created_at=recent,
        updated_at=recent,
        body="Issue details",
        pull_request=None,
        get_comments=lambda: [
            issue_comment,
            old_comment,
            after_range_comment,
            start_boundary_comment,
        ],
    )
    pull_request = SimpleNamespace(
        user=other,
        assignees=[user],
        title="Review request",
        html_url="https://github.com/example/repo/pull/2",
        created_at=stale,
        updated_at=recent,
        body="Other author's PR",
        pull_request={"url": "https://api.github.com/repos/example/repo/pulls/2"},
        repository=repository,
        number=2,
        get_comments=lambda: [],
    )
    merged_pr = SimpleNamespace(
        user=user,
        assignees=[],
        title="Merged change",
        html_url="https://github.com/example/repo/pull/3",
        created_at=recent,
        updated_at=recent,
        body="Merged details",
        pull_request={"url": "https://api.github.com/repos/example/repo/pulls/3"},
        repository=repository,
        number=3,
        get_comments=lambda: [],
    )
    old_bumped_issue = SimpleNamespace(
        user=user,
        assignees=[],
        title="Old issue bumped by others",
        html_url="https://github.com/example/repo/issues/4",
        created_at=stale,
        updated_at=recent,
        body="Old details",
        pull_request=None,
        get_comments=lambda: [
            SimpleNamespace(user=other, created_at=recent, body="Someone else commented"),
            old_comment,
        ],
    )
    queries = []

    def search_issues(query):
        queries.append(query)
        return [authored_issue, pull_request, merged_pr, old_bumped_issue]

    search_hit = SimpleNamespace(get_pulls=lambda: [pull, pull])
    github = SimpleNamespace(search_issues=search_issues, search_commits=lambda query: [search_hit])
    auth = SimpleNamespace(Token=lambda token: token)
    monkeypatch.setitem(
        sys.modules,
        "github",
        SimpleNamespace(Auth=auth, Github=lambda auth: github),
    )

    entries = collect_github(
        Person("A Person", "a@example.com", "aperson"),
        "token",
        cutoff,
        datetime(2026, 10, 1, 23, 59, 59, tzinfo=timezone.utc),
    )

    by_title = {entry.title: entry for entry in entries}
    assert by_title["Current issue"].body == "Issue details"
    assert by_title["Current issue"].author == "aperson"
    assert by_title["Current issue"].authored_in_period
    assert "Old issue bumped by others" not in by_title
    assert by_title["Review request"].body == "Other author's PR"
    assert not by_title["Review request"].authored_in_period
    assert by_title["Current issue"].assignees == ("aperson", "someone-else")
    assert [comment.body for comment in by_title["Current issue"].comments] == [
        "At start",
        "Recent comment",
    ]
    assert all("updated:2026-09-15..2026-10-01" in query for query in queries)
    assert by_title["Review request"].kind == "Pull request"
    assert by_title["Review request"].status == "open"
    assert by_title["Review request"].author == "someone-else"
    assert by_title["Review request"].assignees == ("aperson",)
    assert [comment.body for comment in by_title["Review request"].comments] == ["Review note"]
    assert by_title["Merged change"].status == "merged"
    assert len(queries) == 4
    assert all("is:issue" in query or "is:pull-request" in query for query in queries)
    assert all("author:aperson" in query or "commenter:aperson" in query for query in queries)
    assert not [record for record in caplog.records if record.levelname == "WARNING"]


def test_collect_github_discovers_commit_only_pr_and_filters_commit_activity(monkeypatch):
    cutoff = datetime(2026, 9, 15, tzinfo=timezone.utc)
    end = datetime(2026, 10, 1, 23, 59, 59, tzinfo=timezone.utc)
    user = SimpleNamespace(login="APerson")
    other = SimpleNamespace(login="other")

    def commit(sha, author, when, additions, deletions):
        return SimpleNamespace(
            sha=sha,
            author=author,
            commit=SimpleNamespace(author=SimpleNamespace(date=when), message=f"Change {sha}"),
            stats=SimpleNamespace(additions=additions, deletions=deletions),
        )

    commits = [
        commit("start", user, cutoff, 5, 2),
        commit("end", user, end, 3, 4),
        commit("old", user, datetime(2026, 9, 14, tzinfo=timezone.utc), 9, 9),
        commit("late", user, datetime(2026, 10, 2, tzinfo=timezone.utc), 9, 9),
        commit("other", other, cutoff, 9, 9),
        commit("unlinked", None, cutoff, 9, 9),
    ]
    url = "https://github.com/example/repo/pull/7"
    issue = SimpleNamespace(
        user=other, assignees=[], title="Older PR", html_url=url,
        created_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        body="Context", pull_request={"url": url}, get_comments=lambda: [],
    )
    pull = SimpleNamespace(
        html_url=url, commits=len(commits), additions=99, deletions=88,
        get_commits=lambda: iter(commits), as_issue=lambda: issue,
        is_merged=lambda: False, state="open", draft=False,
        get_review_comments=lambda: [], get_reviews=lambda: [],
    )
    search_hit = SimpleNamespace(get_pulls=lambda: [pull, pull])
    queries = []
    github = SimpleNamespace(
        search_issues=lambda query: [],
        search_commits=lambda query: queries.append(query) or [search_hit],
    )
    monkeypatch.setitem(
        sys.modules, "github",
        SimpleNamespace(Auth=SimpleNamespace(Token=lambda token: token), Github=lambda auth: github),
    )

    entries = collect_github(Person("A Person", "a@example.com", "aperson"), "token", cutoff, end)

    assert queries == ["author:aperson author-date:2026-09-15..2026-10-01"]
    assert len(entries) == 1
    entry = entries[0]
    assert not entry.authored_in_period and not entry.comments
    assert (entry.pr_metrics.commits, entry.pr_metrics.additions, entry.pr_metrics.deletions) == (6, 99, 88)
    assert [(c.sha, c.additions, c.deletions) for c in entry.pr_metrics.period_commits] == [
        ("start", 5, 2), ("end", 3, 4),
    ]
    open_ended = collect_github(
        Person("A Person", "a@example.com", "aperson"), "token", cutoff, as_of=end,
    )
    assert open_ended[0].pr_metrics.period_commits == entry.pr_metrics.period_commits
    commits[0].author = other
    commits[1].author = other
    assert collect_github(
        Person("A Person", "a@example.com", "aperson"), "token", cutoff, end,
    ) == []


@pytest.mark.parametrize("count,listed,expected", [
    (251, 0, "limited to 250"),
    (2, 1, "Incomplete commit list"),
])
def test_pull_request_metrics_reject_incomplete_commit_lists(count, listed, expected):
    pull = SimpleNamespace(
        commits=count, html_url="https://github.com/example/repo/pull/1",
        get_commits=lambda: [None] * listed,
    )
    with pytest.raises(RuntimeError, match=expected):
        _pull_request_metrics(pull, "aperson", datetime(2026, 1, 1, tzinfo=timezone.utc), None)


def test_commit_search_warns_and_stops_at_result_limit(monkeypatch, caplog):
    hit = SimpleNamespace(get_pulls=lambda: [])
    github = SimpleNamespace(
        search_issues=lambda query: [],
        search_commits=lambda query: iter([hit] * 1001),
    )
    monkeypatch.setitem(
        sys.modules, "github",
        SimpleNamespace(Auth=SimpleNamespace(Token=lambda token: token), Github=lambda auth: github),
    )
    entries = collect_github(
        Person("A Person", "a@example.com", "aperson"), "token",
        datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert entries == []
    assert "1,000-result limit" in caplog.text


def test_commit_search_reports_incomplete_results_and_bounds_open_ended_period(monkeypatch, caplog):
    class IncompleteResults:
        incomplete_results = True

        def __iter__(self):
            return iter([])

    queries = []
    github = SimpleNamespace(
        search_issues=lambda query: [],
        search_commits=lambda query: queries.append(query) or IncompleteResults(),
    )
    monkeypatch.setitem(
        sys.modules, "github",
        SimpleNamespace(Auth=SimpleNamespace(Token=lambda token: token), Github=lambda auth: github),
    )
    collect_github(
        Person("A Person", "a@example.com", "aperson"), "token",
        datetime(2026, 9, 15, tzinfo=timezone.utc),
        as_of=datetime(2026, 10, 1, 12, tzinfo=timezone.utc),
    )
    assert queries == ["author:aperson author-date:2026-09-15..2026-10-01"]
    assert "GitHub reported incomplete commit search results" in caplog.text


@pytest.mark.parametrize("date,stats,expected", [
    (None, SimpleNamespace(additions=1, deletions=0), "Missing commit author date"),
    (datetime(2026, 1, 1, tzinfo=timezone.utc), None, "Missing commit statistics"),
])
def test_pull_request_metrics_report_missing_commit_data(date, stats, expected):
    commit = SimpleNamespace(
        sha="abc", author=SimpleNamespace(login="aperson"),
        commit=SimpleNamespace(author=SimpleNamespace(date=date), message="Change"), stats=stats,
    )
    pull = SimpleNamespace(
        commits=1, html_url="https://github.com/x/y/pull/1", get_commits=lambda: [commit],
    )
    with pytest.raises(RuntimeError, match=expected):
        _pull_request_metrics(pull, "aperson", datetime(2026, 1, 1, tzinfo=timezone.utc), None)


def test_fetch_sheet_reads_all_pages():
    calls = []
    first = SimpleNamespace(name="Reports", rows=["row 1", "row 2"])
    second = SimpleNamespace(name="Reports", rows=["row 3"])
    pages = {1: first, 2: second}
    client = SimpleNamespace(Sheets=SimpleNamespace(
        get_sheet=lambda sheet_id, **kwargs: calls.append((sheet_id, kwargs)) or pages[kwargs["page"]]
    ))

    sheet, rows = _fetch_sheet(
        client,
        SheetRef("https://app.smartsheet.com/sheets/123", "123", "https://api.smartsheet.com/2.0"),
        page_size=2,
    )

    assert sheet is first
    assert rows == ["row 1", "row 2", "row 3"]
    assert [call[1]["page"] for call in calls] == [1, 2]


def test_default_config_path_uses_xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    write_config(default_config_path(), [Person("A", "a@example.com", "a")], ["https://app.smartsheet.com/sheets/X1"])

    assert load_users()["a@example.com"].github_user == "a"
    assert load_sheet_urls()[0].sheet_id == "X1"
