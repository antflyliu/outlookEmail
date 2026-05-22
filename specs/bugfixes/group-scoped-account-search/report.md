# Bugfix Report: Group Scoped Account Search

**Date:** 2026-05-19
**Status:** Fixed

## Description of the Issue

After moving Outlook accounts from one group to another, the account search box in the original group could still find the moved accounts.

**Reproduction steps:**
1. Open a normal Outlook group such as `outlook`.
2. Move one or more accounts from that group into another group such as `outlook-注册成功`.
3. Search for the moved account address while still viewing the original group.
4. Observe that the moved account is returned even though it no longer belongs to the selected group.

**Impact:** High for account management. The group list appeared to be scoped, but search results were global, making it easy to select, tag, delete, refresh, or move accounts from the wrong group.

## Investigation Summary

- **Symptoms examined:** The left group count and normal group listing were correct, but search results ignored the selected group.
- **Code inspected:** `static/js/index/02-groups.js`, `outlook_web/segments/04_routes_groups_accounts.py`, `outlook_web/segments/02_groups_accounts.py`, and other `q`/`keyword` query endpoints.
- **Hypotheses tested:** UI cache staleness was ruled out by reproducing the defect directly through `/api/accounts/search?group_id=...&q=...`.

## Discovered Root Cause

`/api/accounts/search` did not read `group_id`, and `search_account_records()` did not pass a group scope into the shared account `WHERE` clause builder. The frontend also called `/api/accounts/search` with only `q`, pagination, sorting, and tag filters.

**Defect type:** Missing filter propagation.

**Why it occurred:** Normal group loading used `/api/accounts?group_id=...`, while search used a separate endpoint and helper. The shared `build_account_where_clause()` already supported `group_id`, but the search path never supplied it.

**Contributing factors:** The UI search field is visually inside the selected group panel, but the endpoint docstring and old behavior treated it as global search.

## Resolution for the Issue

**Changes made:**
- `static/js/index/02-groups.js` - Includes `currentGroupId` as `group_id` when searching normal account groups.
- `outlook_web/segments/04_routes_groups_accounts.py` - Reads `group_id` in `/api/accounts/search` and applies it to both result rows and total count.
- `outlook_web/segments/02_groups_accounts.py` - Allows `search_account_records()` to pass group scope into `build_account_where_clause()`.
- `tests/test_project_runtime.py` - Adds backend and frontend regression coverage.

**Approach rationale:** Reused the existing account filter builder rather than adding a second custom SQL condition. This keeps search, tags, untagged filtering, pagination, and total counts on one consistent filtering path.

**Alternatives considered:**
- Frontend-only filtering - Rejected because pagination and totals would remain wrong, and accounts outside the current page could still leak through.
- New dedicated search SQL branch - Rejected because `build_account_where_clause()` already expresses the correct group/query/tag composition.

## Regression Test

**Test file:** `tests/test_project_runtime.py`

**Test names:**
- `test_account_search_is_scoped_to_requested_group`
- `test_account_search_request_includes_current_group_scope`

**What it verifies:** A search request scoped to the source group does not return an account that has been moved to a target group, and the frontend search request includes the current group scope before fetching `/api/accounts/search`.

**Run command:** `python -m unittest discover -s tests -p test_project_runtime.py -v`

## Affected Files

| File | Change |
|------|--------|
| `outlook_web/segments/02_groups_accounts.py` | Search helper now accepts group scope and passes it to the shared account where-clause builder. |
| `outlook_web/segments/04_routes_groups_accounts.py` | Search route now reads `group_id` and uses it for rows and totals. |
| `static/js/index/02-groups.js` | Account search requests now include the selected group id. |
| `tests/test_project_runtime.py` | Added backend and frontend regression tests. |

## Verification

**Automated:**
- [x] Regression failure confirmed before the fix: `source_payload['total']` was `1` instead of `0`.
- [x] `python -m unittest discover -s tests -p test_project_runtime.py -v` passes with 49 tests.
- [x] `python -m unittest discover -s tests -p test_docker_update.py -v` passes with 18 tests.
- [x] `python -m unittest discover -s tests -p test_error_handling.py -v` passes with 1 test.
- [x] `python -m unittest discover -s tests -p test_runtime_env.py -v` passes with 1 test.
- [x] `python -m py_compile outlook_web\segments\02_groups_accounts.py outlook_web\segments\04_routes_groups_accounts.py tests\test_project_runtime.py` passes.
- [x] `node --check static\js\index\02-groups.js` passes.
- [ ] Full test suite passes. `python -m unittest discover -s tests -v` is blocked by existing test isolation issues after `test_docker_update.py` leaves the imported app pointing at a deleted `.tmp/docker-update-tests-*` database path, causing later tests to fail with `sqlite3.OperationalError: unable to open database file`.
- [x] `python -m unittest discover -s tests -p test_imap_folder_resolution.py -v` passes with 70 tests after restoring external account refresh-log fallback serialization.
- [ ] Linters pass. No project lint command is defined in the repository.

**Manual verification:**
- Reviewed other query endpoints. `/api/projects/<project_key>/accounts` already passes `group_id` into `load_project_accounts`; `/api/accounts/refresh-status-list` is a global refresh-management query and is not tied to the selected group panel.

## Prevention

**Recommendations to avoid similar bugs:**
- Account-list endpoints with `q`, tag filters, pagination, or totals should route all filters through `build_account_where_clause()` when they are displayed inside a selected group context.
- Frontend requests from the group account panel should always include `currentGroupId` unless the selected group is the temporary email group.
- Keep backend route tests paired with lightweight frontend request-shape tests for scoped list behavior.

## Related

- User report: accounts moved from `outlook` into `outlook-注册成功` were still searchable from `outlook`.
