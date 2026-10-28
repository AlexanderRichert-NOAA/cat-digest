from datetime import datetime, timezone

from cat_digest.core import Person
from cat_digest.document import render_document, write_document
from cat_digest.integrations import GitHubComment, GitHubEntry


def test_document_groups_each_person_smartsheet_before_github():
    users = {"a@example.com": Person("A Person", "a@example.com", "aperson")}
    report = {
        "sheet": "Weekly report",
        "url": "https://example.com/sheet",
        "fields": {
            "Submitted By": "A Person",
            "Date Submitted": "2026-10-01",
            "Task Name": "Triage queue",
            "Progress Report": "Finished",
        },
    }
    issue = GitHubEntry(
        kind="Pull request",
        title="Fix the workflow",
        url="https://github.com/example/repo/issues/1",
        created_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
        body="Details",
        author="aperson",
        assignees=("aperson", "someone-else"),
        comments=(GitHubComment("aperson", datetime(2026, 10, 2, tzinfo=timezone.utc), "Agreed", "Comment"),),
        status="merged",
    )

    text = render_document(
        users,
        {"a@example.com": [report]},
        {"a@example.com": [issue]},
        datetime(2026, 10, 15, tzinfo=timezone.utc),
    )

    assert text.index("SMARTSHEET REPORTS") < text.index("Weekly report") < text.index("GITHUB")
    assert "Task Name: Triage queue" in text
    assert "Progress Report: Finished" in text
    assert "Fix the workflow" in text
    assert "Author: A Person" in text
    assert "Assignees: A Person, someone-else" in text
    assert "Status: merged" in text
    assert "Comment (2026-10-02): Agreed" in text


def test_document_omits_disabled_sources():
    users = {"a@example.com": Person("A Person", "a@example.com", "aperson")}

    text = render_document(
        users,
        {"a@example.com": [{"sheet": "Report", "url": "url", "fields": {}}]},
        {},
        datetime(2026, 10, 15, tzinfo=timezone.utc),
        include_smartsheet=False,
        include_github=True,
    )

    assert "SMARTSHEET REPORTS" not in text
    assert "GITHUB" in text


def test_write_document_creates_utf8_text_file(tmp_path):
    output = tmp_path / "nested" / "CAT-Digest.txt"
    content = "CAT Digest\nRésumé complete\n"

    saved_path = write_document(output, content)

    assert saved_path == output
    assert output.read_text(encoding="utf-8") == content


def test_parse_digest_splits_people_and_reads_period():
    from datetime import date

    from cat_digest.document import parse_digest

    text = (
        "CAT Digest | 2026-10-15\nPeriod: 2026-09-15 to 2026-10-15\n\n"
        "Ann One\n=======\n\nSMARTSHEET REPORTS\n\nNo matching Smartsheet reports.\n\n"
        "Bob Two\n=======\n\nGITHUB\n\nstuff\n"
    )

    sections, period = parse_digest(text)

    assert list(sections) == ["Ann One", "Bob Two"]
    assert "stuff" in sections["Bob Two"] and "stuff" not in sections["Ann One"]
    assert period == (date(2026, 9, 15), date(2026, 10, 15))


def test_document_labels_background_items_that_are_not_period_work():
    users = {"a@example.com": Person("A Person", "a@example.com", "aperson")}
    old = GitHubEntry(
        kind="Issue",
        title="Old issue",
        url="https://github.com/example/repo/issues/9",
        created_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        body="Original report",
        author="aperson",
        assignees=(),
        comments=(GitHubComment("aperson", datetime(2026, 10, 2, tzinfo=timezone.utc), "Update", "Comment"),),
        authored_in_period=False,
    )

    text = render_document(
        users, {}, {"a@example.com": [old]}, datetime(2026, 10, 15, tzinfo=timezone.utc)
    )

    assert "Background context only, NOT work done in this period (item created 2026-08-01" in text
    assert "Original report" in text
    assert "   Created:" not in text
    assert "Comment (2026-10-02): Update" in text
