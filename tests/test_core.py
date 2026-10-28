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
from cat_digest.integrations import _fetch_sheet, collect_github
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


def test_collect_github_keeps_recent_user_comments_and_reviews(monkeypatch):
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
        get_review_comments=lambda: [review_comment],
        get_reviews=lambda: [],
    )
    merged_pull = SimpleNamespace(
        is_merged=lambda: True,
        state="closed",
        draft=False,
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

    github = SimpleNamespace(search_issues=search_issues)
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
