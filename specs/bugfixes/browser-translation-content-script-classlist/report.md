# Bugfix Report: Browser Translation Content Script ClassList Error

**Date:** 2026-05-28
**Status:** Fixed

## Description of the Issue

The browser console showed repeated `Uncaught (in promise) TypeError: Cannot read properties of null (reading 'classList')` errors while the Outlook mail UI was open on `127.0.0.1:5000`.

**Reproduction steps:**
1. Open the local Outlook mail UI in a browser with translation extensions enabled.
2. Select a group and let the account list render dynamic cards.
3. Observe repeated console errors from `content_main.js`.

**Impact:** The errors polluted the console and made it harder to identify real application-side JavaScript failures.

## Investigation Summary

- **Symptoms examined:** The stack trace pointed to `content_main.js`, not any script under `static/js/index/`.
- **Code inspected:** `templates/index.html`, `templates/partials/index/layout.html`, and all local frontend JavaScript references to `classList`.
- **Hypotheses tested:** A missing local DOM null guard was considered, but `content_main.js` is not present in the repository and is consistent with a browser extension content script.

## Discovered Root Cause

The page did not explicitly opt out of browser translation overlays. Translation content scripts can scan and mutate dynamic application DOM while the account list is being re-rendered, and the injected script can then dereference a stale/null element.

**Defect type:** Missing page-level integration guard for browser translation overlays.

**Why it occurred:** The application is a Chinese-language operational UI, so its dynamic DOM is a tempting target for translation extensions even though the page should be treated as an application surface rather than translatable article content.

**Contributing factors:** The UI frequently re-renders group/account/email panels, increasing the chance that injected scripts observe stale DOM nodes.

## Resolution for the Issue

**Changes made:**
- `templates/index.html` - Added `class="notranslate"` and `translate="no"` to the root `<html>` element.
- `templates/index.html` - Added `<meta name="google" content="notranslate">`.
- `tests/test_project_runtime.py` - Added a regression test that locks the page-level no-translation contract.

**Approach rationale:** The stack trace belongs to an injected content script, so the safest project-side fix is to opt the app out of automatic translation overlays instead of adding unrelated null guards to local application code.

**Alternatives considered:**
- Add null guards around local `classList` usage - rejected because the failing script is not part of this repository.
- Ignore the console error as extension-only - rejected because the app can provide standard no-translation hints to reduce the failure.

## Regression Test

**Test file:** `tests/test_project_runtime.py`
**Test name:** `test_index_page_disables_browser_translation_overlays`

**What it verifies:** The logged-in app entry template keeps the root `notranslate` and `translate="no"` attributes plus the `google=notranslate` meta tag.

**Run command:** `python -m pytest tests/test_project_runtime.py::FrontendTimezoneBootstrapTests::test_index_page_disables_browser_translation_overlays -q`

## Affected Files

| File | Change |
|------|--------|
| `templates/index.html` | Opted the app page out of browser translation overlays. |
| `tests/test_project_runtime.py` | Added a regression test for the no-translation page contract. |

## Verification

**Automated:**
- [x] Regression test passes.
- [x] `FrontendTimezoneBootstrapTests` passes.
- [x] `tests/test_project_runtime.py` passes.
- [ ] Full repository test suite was not run.

**Manual verification:**
- Confirmed the reported `content_main.js` file is not present in the repository and local scripts are loaded from `static/js/index/*.js`.

## Prevention

**Recommendations to avoid similar bugs:**
- Keep application-shell pages marked as non-translatable when browser translation overlays are not part of the supported runtime.
- Treat future console stacks from extension content scripts separately from local `static/js` stacks before editing application logic.

