# CAT Digest

Collects recent GitHub content (issues, pull requests, comments) for specified user(s), and provides an LLM-generated summary of work for each user using the Google Gemini API.

## Getting started

First, set the following API keys as environment variables, or provided when prompted by `cat-digest`:

- `GITHUB_TOKEN`: Fine-grained token with read access for Issues, Pull requests,
  and Contents on the target repositories.
- `SMARTSHEET_ACCESS_TOKEN`: Smartsheet API token (only needed if querying Smartsheet reports).
- `GEMINI_API_KEY`: Google AI Studio key, needed only when generating LLM reports.
  Not available through NOAA Google Workspace.

```sh
python -m venv .venv
. .venv/bin/activate
pip install -e .
cat-digest
```

On first run, CAT Digest offers to create your config (users and Smartsheet URLs) at `~/.config/cat-digest/config.yaml`. Results are saved under `catdigests/`.

## Configuration

Everything lives in `~/.config/cat-digest/config.yaml` (respects `XDG_CONFIG_HOME`); create it with `cat-digest --init-config`. `--init-config` creates a single user; for multiple users, edit the file by hand and add entries under `users:`:

```yaml
users:
  - name: Example Person
    email: example.person@noaa.gov
    github_user: example-user
  - name: Another Person
    email: another.person@noaa.gov
    github_user: another-user
smartsheet_report_urls:
  - https://app.smartsheetgov.com/sheets/AbC123
```

Smartsheet rows are matched to people by `Submitted By` name or a contact/email column. Rows without a recognized date or matching person are omitted.

## Options

| Option | Description |
|---|---|
| `--init-config` | Create the config interactively, then exit. |
| `--user NAME\|EMAIL\|GITHUB_USER` | Select a configured user (repeatable). Default: prompt, or all users without a terminal. |
| `--all-users` | Use every configured user without prompting. |
| `--[no-]smartsheet`, `--[no-]github` | Include or omit a source. |
| `--smartsheet-days N`, `--github-days N` | Lookback per source (default 30). |
| `--start-date`, `--end-date` | Inclusive `YYYY-MM-DD` range for both sources; use together. Overrides lookback days. |
| `--smartsheet-date-column TITLE` | Date column to use if auto-detection picks the wrong one. |
| `--output-dir DIR` | Where output is written (default `catdigests/`). Existing files and directories are never overwritten or deleted; a colliding run directory gets a `-2`, `-3`, … suffix. |
| `--[no-]llm` | Generate (or skip) per-person LLM reports. |
| `--llm-prompts-only` | Save LLM prompts without querying Gemini or requiring a Gemini API key or model selection. Cannot be combined with `--no-llm`. |
| `--print-llm-prompts` | Also print each saved LLM prompt to stdout, delimited by its filename. Cannot be combined with `--no-llm`. |
| `--llm-config FILE` | Use this LLM settings file instead of the defaults below. |
| `--model MODEL[/LEVEL]` | Gemini model and thinking level, e.g. `gemini-3.8-flash/low`. Required without a terminal. |
| `--summarize-from DIR\|FILE` | Generate missing LLM reports from an existing digest without re-collecting. Add `--start-date`/`--end-date` for digests that lack a period. |
| `--debug` | Show detailed requests and progress. Review the output for credentials before sharing. |

Example: `cat-digest --user person@example.com --smartsheet-days 14 --no-github`

## Output

Each run creates `CAT-Digest-YYYY-MM-DD-HH-MMZ/` (UTC) under the output directory, containing the raw digest `CAT-Digest-<timestamp>.txt` and one LLM report per person, e.g. `John-Smith.txt`; each report is also printed to stdout. The raw digest is saved first, so if the Gemini API fails you can finish later with `--summarize-from`.

Each LLM request also saves the system prompt followed by the person's raw input
as plain text in `John-Smith-llm-prompt.txt`, ready to copy and paste. The file
is saved before sending the request, even if the request fails; retries and later
runs use numbered filenames without overwriting earlier prompts. Model and
generation settings are available with `--debug`; API keys are not saved.
Prompt files contain the same potentially sensitive content sent to Gemini, so
handle them as you would the raw digest. No prompt file is created when LLM
generation is skipped.

Use `--llm-prompts-only` to save prompts without generating reports; it also works
with `--summarize-from` and creates prompts for selected people even if their
reports already exist. Existing prompt files are never overwritten. The model
setting and `--model` have no effect in this mode.
Add `--print-llm-prompts` to print the saved prompts to stdout for copying; it
works with both prompt-only and normal report generation. Prompts can contain
sensitive data, so use care when capturing or sharing stdout.

To customize, copy the packaged `llm.toml` (in the `cat_digest` package) to `~/.config/cat-digest/llm.toml` and keep only the keys you want to change (it overrides the packaged defaults key by key). You can change the prompt (`{name}`, `{start_date}`, `{end_date}` are substituted), default model, temperature, thinking levels, and hidden models (`exclude_models`, default `gemini-2.5-*`).

Raw content, including issue text, is sent to Google for summarization; use `--no-llm` to avoid this.

### GitHub pull request metrics

The raw digest includes GitHub's current whole-PR commit count and diff
additions/deletions as context. These snapshots cover all contributors and may
include activity after the reporting period, especially for historical reports.
Separately, it lists the selected person's commits whose GitHub-linked author
and author timestamp fall within the period, with their count and summed
per-commit additions/deletions. Commit author dates need not be push dates;
commits without a linked GitHub author cannot be attributed. Summed commit
changes are churn, not the net PR diff: the same lines may be counted repeatedly.
Neither metric alone reliably measures effort.
The packaged LLM prompt treats whole-PR totals as context only; custom prompt
overrides should preserve that distinction and restrict work claims to dated,
in-period activity.

Commit-only PR discovery is best-effort. GitHub's global commit search indexes
only default branches, so activity solely on an open PR branch can be missed
unless the PR is found through the existing author/commenter searches. Search
indexing, inaccessible repositories, unlinked authors, rewritten commits, and
commit-to-PR association can also affect discovery. Search may return incomplete
results and is capped at 1,000 results; the application warns about these limits.
GitHub limits PR commit listings to 250 commits; collection fails explicitly
instead of reporting incomplete per-person totals when that limit is exceeded
or the listing does not match the PR's commit count. Commit searches, PR commit
listings, and per-commit statistics add API requests and can incur rate limits.

## Tests

```sh
pytest
```
