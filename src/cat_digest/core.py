from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse

import yaml

LOG = logging.getLogger("cat_digest")


@dataclass(frozen=True)
class Person:
    name: str
    email: str
    github_user: str


@dataclass(frozen=True)
class SheetRef:
    url: str
    sheet_id: str
    api_base: str


def _value(obj: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(obj, Mapping) and name in obj:
            return obj[name]
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


def parse_sheet_url(url: str) -> SheetRef:
    parsed = urlparse(url.strip())
    match = re.fullmatch(r"/sheets/([A-Za-z0-9]+)(?:/)?", parsed.path)
    if parsed.scheme != "https" or not parsed.hostname or not match:
        raise ValueError(f"not a supported Smartsheet sheet URL: {url}")
    host = parsed.hostname.casefold()
    if host == "smartsheetgov.com" or host.endswith(".smartsheetgov.com"):
        api_base = "https://api.smartsheetgov.com/2.0"
    elif host == "smartsheet.com" or host.endswith(".smartsheet.com"):
        api_base = "https://api.smartsheet.com/2.0"
    else:
        raise ValueError(f"unsupported Smartsheet host: {parsed.hostname}")
    return SheetRef(url=url.strip(), sheet_id=match.group(1), api_base=api_base)


def parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _date_column(columns: dict[Any, str], requested: str | None) -> Any | None:
    if requested:
        return next((key for key, title in columns.items() if title.casefold() == requested.casefold()), None)
    priorities = (
        ("report date", "date reported", "date submitted", "submitted date"),
        ("submitted on", "submitted at", "created on", "created at"),
        ("date", "time", "timestamp"),
        ("updated date", "modified date", "updated at", "modified at"),
    )
    for terms in priorities:
        for key, title in columns.items():
            normalized = title.casefold()
            if any(term in normalized for term in terms) and "due" not in normalized:
                return key
    return None


def _cell_text(cell: Any) -> str:
    display = _value(cell, "display_value", "displayValue")
    value = _value(cell, "value")
    raw = display if display not in (None, "") else value
    if raw is None:
        raw = _value(cell, "object_value", "objectValue")
    if isinstance(raw, Mapping):
        return ", ".join(f"{key}: {val}" for key, val in raw.items() if val not in (None, ""))
    if isinstance(raw, (list, tuple)):
        return ", ".join(str(item) for item in raw)
    return str(raw) if raw is not None else ""


def _raw_cell(cell: Any) -> Any:
    to_dict = getattr(cell, "to_dict", None)
    return to_dict() if callable(to_dict) else cell


def row_to_fields(row: Any, columns: dict[Any, str]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for cell in _value(row, "cells", default=[]) or []:
        column_id = _value(cell, "column_id", "columnId")
        title = columns.get(column_id, f"Column {column_id}")
        text = _cell_text(cell).strip()
        if text:
            fields[title] = text
        else:
            LOG.debug("Smartsheet empty cell %r: %.300s", title, _raw_cell(cell))
    return fields


def _row_people(row: Any, columns: dict[Any, str], users: Mapping[str, Person]) -> set[str]:
    matches: set[str] = set()
    names = {" ".join(person.name.casefold().split()): email for email, person in users.items()}
    for cell in _value(row, "cells", default=[]) or []:
        column_id = _value(cell, "column_id", "columnId")
        title = columns.get(column_id, "").casefold()
        candidates = [_value(cell, "value")]
        object_value = _value(cell, "object_value", "objectValue")
        candidates.extend((_value(object_value, "email"), _value(object_value, "name")))
        if "email" in title or "contact" in title:
            candidates.append(_cell_text(cell))
        for candidate in candidates:
            if not isinstance(candidate, str):
                continue
            normalized = " ".join(candidate.casefold().split())
            if "@" in candidate and normalized in users:
                matches.add(normalized)
            elif "submitted by" in title or "contact" in title:
                email = names.get(normalized)
                if email:
                    matches.add(email)
    return matches


def filter_smartsheet_rows(
    rows: Iterable[Any],
    columns: Iterable[Any],
    users: Mapping[str, Person],
    cutoff: datetime,
    date_column: str | None = None,
    end_date: datetime | None = None,
) -> dict[str, list[dict[str, str]]]:
    column_titles = {
        _value(column, "id"): str(_value(column, "title", default="")).strip()
        for column in columns
    }
    date_id = _date_column(column_titles, date_column)
    if date_column and date_id is None:
        raise ValueError(f"date column not found: {date_column}")
    cutoff_utc = parse_datetime(cutoff)
    end_date_utc = parse_datetime(end_date) if end_date is not None else None
    results = {email: [] for email in users}
    for row in rows:
        cells = _value(row, "cells", default=[]) or []
        date_cell = next((cell for cell in cells if _value(cell, "column_id", "columnId") == date_id), None)
        report_date = parse_datetime(_value(date_cell, "value")) if date_cell is not None else None
        if (
            report_date is None
            or report_date < cutoff_utc
            or (end_date_utc is not None and report_date > end_date_utc)
        ):
            continue
        fields = row_to_fields(row, column_titles)
        fields[column_titles[date_id]] = report_date.date().isoformat()
        for email in _row_people(row, column_titles, users):
            results[email].append(fields)
    return results


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "cat-digest"


def default_config_path() -> Path:
    return config_dir() / "config.yaml"


def load_config(path: str | Path | None = None) -> tuple[dict[str, Person], list[SheetRef]]:
    path = Path(path) if path else default_config_path()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"config file {path} not found; run `cat-digest --init-config`") from None
    except yaml.YAMLError as exc:
        raise ValueError(f"config file {path} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"config file {path} must be a mapping")
    raw_users = data.get("users")
    if not isinstance(raw_users, list) or not raw_users:
        raise ValueError(f"config file {path}: users must be a non-empty list")
    users: dict[str, Person] = {}
    for index, entry in enumerate(raw_users, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"config file {path}: user {index} must be a mapping")
        values = {key: str(entry.get(key) or "").strip() for key in ("name", "email", "github_user")}
        missing = [key for key, value in values.items() if not value]
        if missing:
            raise ValueError(f"config file {path}: user {index} is missing: {', '.join(missing)}")
        person = Person(**values)
        key = person.email.casefold()
        if key in users:
            raise ValueError(f"config file {path}: duplicate email address: {person.email}")
        users[key] = person
    urls = data.get("smartsheet_report_urls") or []
    if not isinstance(urls, list):
        raise ValueError(f"config file {path}: smartsheet_report_urls must be a list")
    refs = []
    for url in urls:
        try:
            refs.append(parse_sheet_url(str(url)))
        except ValueError as exc:
            raise ValueError(f"config file {path}: {exc}") from exc
    return users, refs


def load_users() -> dict[str, Person]:
    return load_config()[0]


def load_sheet_urls() -> list[SheetRef]:
    refs = load_config()[1]
    if not refs:
        raise ValueError(f"no smartsheet_report_urls in {default_config_path()}")
    return refs


def write_config(path: str | Path, people: list[Person], urls: list[str]) -> Path:
    for url in urls:
        parse_sheet_url(url)
    if not people or not all(all((p.name, p.email, p.github_user)) for p in people):
        raise ValueError("each user needs a name, email, and GitHub handle")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "users": [
            {"name": p.name, "email": p.email, "github_user": p.github_user} for p in people
        ],
        "smartsheet_report_urls": [url.strip() for url in urls],
    }
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path
