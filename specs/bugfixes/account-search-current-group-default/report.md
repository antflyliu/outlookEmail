# Bugfix Report: Account Search Current Group Default

**Date:** 2026-05-24
**Status:** Fixed

## Description of the Issue

After moving an account into another group, searching that account from the source group's account search box could still return it. The observed request was:

`/api/accounts/search?q=dhpw&offset=0&limit=200&sort_by=sort_order&sort_order=asc`

It did not include `group_id`, so the backend correctly treated it as a global search.

**Reproduction steps:**
1. Open a normal mailbox group.
2. Move an account from that group into another group.
3. Search the moved account while still viewing the original group.
4. Observe that the request can omit `group_id` and return a global result.

**Impact:** Account management in a selected group could show accounts that no longer belong to that group, especially after batch move operations.

## Investigation Summary

- **Symptoms examined:** The user-provided search URL lacked `group_id`; the UI also showed the search scope set to "all groups".
- **Code inspected:** `templates/partials/index/layout.html`, `static/js/index/02-groups.js`, `static/js/index/10-batch-actions.js`, and `/api/accounts/search` in `outlook_web/segments/04_routes_groups_accounts.py`.
- **Hypotheses tested:** Backend filter support was verified with `/api/accounts/search?...&group_id=...`; the defect was not a missing backend parameter handler.

## Discovered Root Cause

The backend already supported `group_id`, but the frontend search scope defaulted to all groups and existing browser `localStorage` could preserve that global search mode. In that state, `searchAccounts()` intentionally omitted `group_id`.

**Defect type:** UI default and persisted-state regression.

**Why it occurred:** The account panel added an explicit "all groups/current group" selector, but made "all groups" the default even though the search box is visually scoped inside the selected group panel.

**Contributing factors:** Batch move refreshed the visible list but only cleared the current group's cache, leaving other account-list caches potentially stale after cross-group moves.

## Resolution for the Issue

**Changes made:**
- `templates/partials/index/layout.html` - Makes "current group" the selected search scope in the account panel.
- `static/js/index/02-groups.js` - Adds a one-time migration that resets old default search scope state to `group`, while preserving later explicit user changes.
- `static/js/index/10-batch-actions.js` - Invalidates all normal account caches after batch group moves.
- `tests/test_project_runtime.py` - Adds regression coverage for the default search scope, `group_id` request propagation, and cache invalidation after batch move.

**Approach rationale:** Keep backend global search available, but make the selected group panel behave as scoped by default. This matches the user's workflow and the visual location of the search box.

**Alternatives considered:**
- Force every search request to include `group_id` - Rejected because the explicit "all groups" selector remains a valid global search mode.
- Remove the search scope selector - Rejected because global account search was an existing feature.

## Regression Test

**Test file:** `tests/test_project_runtime.py`

**Test names:**
- `test_account_search_can_filter_to_group`
- `test_account_search_ui_defaults_to_current_group_scope`

**What it verifies:** Backend search honors `group_id`, the account search UI defaults to current-group scope, existing stored all-group defaults are migrated once, and batch moves invalidate normal account caches.

**Run command:** `python -m unittest discover -s tests -p test_project_runtime.py -k "test_account_search" -v`

## Affected Files

| File | Change |
|------|--------|
| `templates/partials/index/layout.html` | Current-group search is now the default option. |
| `static/js/index/02-groups.js` | Migrates legacy all-group default to current-group search once. |
| `static/js/index/10-batch-actions.js` | Clears all normal account caches after moving accounts between groups. |
| `tests/test_project_runtime.py` | Adds focused regression coverage. |
| `specs/bugfixes/account-search-current-group-default/report.md` | Records investigation, root cause, fix, and verification. |

## Verification

**Automated:**
- [x] Regression failure confirmed before implementation: the new UI test failed because the template selected "all groups".
- [x] `python -m unittest discover -s tests -p test_project_runtime.py -k "test_account_search" -v`
- [x] `python -m unittest discover -s tests -p test_project_runtime.py -v`
- [x] `python -m py_compile outlook_web\segments\02_groups_accounts.py outlook_web\segments\04_routes_groups_accounts.py tests\test_project_runtime.py`
- [x] `node --check static\js\index\02-groups.js`
- [x] `node --check static\js\index\10-batch-actions.js`

**Manual verification:**
- Reviewed the generated request path: current-group scope causes `searchAccounts()` to add `group_id` to `/api/accounts/search`.

## Prevention

**Recommendations to avoid similar bugs:**
- Treat account-panel search as current-group scoped unless the user explicitly selects global search.
- Pair backend query tests with frontend request-shape tests whenever a UI control changes API scope.
- Clear all affected list caches after cross-group mutations.

## Related

- Prior report: `specs/bugfixes/group-scoped-account-search/report.md`
