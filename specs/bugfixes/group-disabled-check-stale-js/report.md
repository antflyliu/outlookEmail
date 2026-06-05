# Group disabled check stale JS handler bugfix

## Status

Fixed.

## Problem

After restarting the project, clicking the account-panel magnifier button for "检测当前分组并标注停用" opened the generic "批量停用检测" dialog and did not call the new group endpoint:

`POST /api/groups/<group_id>/accounts/disabled-check`

## Root Cause

The page loaded split frontend files from `/static/js/index/*.js` without any cache-busting query string. Browsers could keep executing stale copies of `02-groups.js` and `10-batch-actions.js` after a server restart, so the visible button could still be bound to old behavior and never invoke the new group disabled-check request path.

The shared disabled-check modal also kept the same "批量停用检测" title for group scans, making it look like the old manual batch-flow even when the new handler was active.

## Fix

- Added `static_asset_version(filename)` as a Jinja helper that uses each static file's `st_mtime_ns`.
- Added per-file `?v=...` query strings to every split index JS script in `templates/index.html`.
- Added `disabledCheckModalTitle` and `setDisabledCheckModalMode()`.
- The manual batch path now restores "批量停用检测" mode.
- The group one-click path now switches the modal to "当前分组停用检测", hides TXT/email manual input controls, and then calls the group endpoint.
- Changed the group endpoint to start a background task by default and return `202 + task_id` immediately.
- Added `GET /api/groups/disabled-check-tasks/<task_id>` for frontend polling.
- Kept a synchronous diagnostic path via `{"async": false}` and `/api/groups/<group_id>/accounts/disabled-check-sync`.
- Added explicit `[disabled-check] task accepted/started/completed/failed` console logs so receipt is visible before the long scan finishes.
- The background task now scans group accounts with a bounded `ThreadPoolExecutor`, defaulting to 6 concurrent account checks and configurable via `DISABLED_GROUP_CHECK_MAX_WORKERS`.
- For the current Windows + SQLite runtime, group proxy configuration is read once before worker dispatch. Account workers do not open Flask app contexts or read SQLite for proxy lookup; the final `inactive` status update remains a single main-task DB update after all workers finish.
- Fixed the preloaded-proxy scan path to call `fetch_account_emails_with_proxy()` directly. Without this split, workers still fell back into `fetch_account_emails()`, which reads proxy settings from DB and can fail immediately outside a Flask app context, making the dialog show a fast but non-real scan result.
- Added persistent history for group disabled-check tasks in SQLite via `group_disabled_check_tasks`, including task status, group name, summary counts, payload JSON, and timestamps.
- Added `GET /api/groups/disabled-check-tasks/history` for task-history listing and made the task-detail endpoint fall back to persisted history after in-memory task state expires or the process restarts.
- Added a locked running state for the group disabled-check modal: while a task is being started/polled, backdrop clicks no longer close it, the page remains blocked behind the modal, and the loading view uses a spinner with clearer progress copy.
- Added task-history entry points in the account-panel toolbar and disabled-check modal footer, plus a task-history modal that can reopen persisted task results.

## Follow-up Root Cause

The browser Network panel showed the `disabled-check` request with provisional headers and no response headers. This was not CORS: page and request were both on `http://127.0.0.1:5000`.

The request was a long-running synchronous scan. For a group with many Outlook accounts, each account can attempt Graph and IMAP fallback token paths; if tokens are invalid or tenant-scoped incorrectly, one POST can stay pending for minutes. Flask access logs appear when the response is completed, so the backend can appear to have received nothing.

The fix makes request receipt explicit and fast: the POST now logs task acceptance and returns immediately, while the expensive scan continues in a daemon worker and the UI polls for completion.

## Verification

```powershell
python -m pytest tests/test_project_runtime.py -k "disabled_check or index_page_disables_browser_translation_overlays"
python -m pytest -k "disabled_check or disabled_notice"
python -m pytest tests/test_project_runtime.py -k "group_disabled_check or batch_disabled_check_ui"
```

Results:

- 4 selected tests passed in `tests/test_project_runtime.py`.
- 9 selected disabled-check tests passed across `tests/test_imap_folder_resolution.py` and `tests/test_project_runtime.py`.
- 9 selected group/UI/init-db tests passed in `tests/test_project_runtime.py`, including regression tests for task history persistence, persisted detail fallback, overlapping account scans, and preloaded proxy usage.
- 63 runtime tests passed in `tests/test_project_runtime.py`.
- `node --check static/js/index/10-batch-actions.js` passed.

## Notes

The running Flask process must be restarted once for the new template helper and script URLs to be served. After that, the browser will request new JS URLs whenever the underlying split JS file changes.
