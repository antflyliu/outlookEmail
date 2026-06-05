# Bugfix Report: External Emails Keyword Graph Body

**Date:** 2026-05-26
**Status:** Fixed

## Description of the Issue

Cockpit Tools "测试 OTP" calls the local outlookEmail endpoint:

`GET /api/external/emails?email=<email>&folder=all&top=20&keyword=code`

When an OAuth/Graph account had no keyword match in the message subject or body preview, outlookEmail returned HTTP 500:

`cannot access local variable 'detail' where it is not associated with a value`

**Reproduction steps:**
1. Configure Cockpit Tools automatic reauthorization to use outlookEmail.
2. Run "测试 OTP" for an Outlook account whose candidate email requires body lookup for `keyword=code`.
3. Observe outlookEmail returning 500 from `email_matches_filters`.

**Impact:** OTP retrieval failed for valid OAuth/Graph accounts whenever the OTP keyword was only present in the full email body.

## Investigation Summary

- **Symptoms examined:** Cockpit Tools UI error and Flask traceback from `api_external_get_emails_v2` into `email_matches_filters`.
- **Code inspected:** `outlook_web/segments/08_forwarding_scheduler_errors.py` external emails filtering and existing external API tests in `tests/test_imap_folder_resolution.py`.
- **Hypotheses tested:** API key/config failure was ruled out because the request reached the authenticated endpoint and failed inside keyword filtering. Missing account was ruled out because the stack passed through `fetch_account_emails` and item filtering.

## Discovered Root Cause

`email_matches_filters` correctly returns early for IMAP accounts after attempting an IMAP detail lookup. The OAuth/Graph detail lookup was accidentally indented inside the IMAP branch after `return False`, making it unreachable. Non-IMAP accounts then reached `if not detail:` without any local `detail` assignment.

**Defect type:** Logic error / unreachable code caused by incorrect indentation.

**Why it occurred:** The function has two account-specific detail lookup paths. A later edit placed the Graph lookup under the IMAP block, but Python's local-variable analysis still treated `detail` as a local name for the whole function.

**Contributing factors:** Existing tests covered external email list resolution and plus-address fallback, but not keyword filtering when the keyword exists only in the Graph email body.

## Resolution for the Issue

**Changes made:**
- `outlook_web/segments/08_forwarding_scheduler_errors.py:1481` - moved `get_email_detail_graph` back to the OAuth/Graph branch after the IMAP early return.
- `tests/test_imap_folder_resolution.py:1178` - added a regression test proving `/api/external/emails?...&keyword=code` loads Graph body content when subject/preview do not match.

**Approach rationale:** This is the smallest behavioral fix: preserve IMAP handling, restore the existing Graph fallback, and avoid changing the public API contract.

**Alternatives considered:**
- Initialize `detail = None` at function start - rejected because it would hide the indentation bug and make Graph body keyword matching silently fail.
- Search only subject/body preview for external OTP - rejected because OTP emails often require full body lookup.

## Regression Test

**Test file:** `tests/test_imap_folder_resolution.py`
**Test name:** `test_external_emails_keyword_filter_checks_graph_body`

**What it verifies:** External email keyword filtering for OAuth/Graph accounts calls `get_email_detail_graph` and keeps the message when the keyword is present only in the full HTML body.

**Run command:** `python -m pytest tests/test_imap_folder_resolution.py::ExternalAccountsApiTests::test_external_emails_keyword_filter_checks_graph_body -q`

## Affected Files

| File | Change |
|------|--------|
| `outlook_web/segments/08_forwarding_scheduler_errors.py` | Restored Graph body lookup path for external keyword filtering. |
| `tests/test_imap_folder_resolution.py` | Added focused regression coverage for the 500 error. |
| `specs/bugfixes/external-emails-keyword-graph-body/report.md` | Documented root cause, fix, and verification. |

## Verification

**Automated:**
- [x] Regression test passes: `python -m pytest tests/test_imap_folder_resolution.py::ExternalAccountsApiTests::test_external_emails_keyword_filter_checks_graph_body -q`
- [x] External accounts API tests pass: `python -m pytest tests/test_imap_folder_resolution.py::ExternalAccountsApiTests -q`
- [x] Syntax validation passes: `python -m py_compile outlook_web/segments/08_forwarding_scheduler_errors.py`

**Manual verification:**
- Reviewed the user-provided traceback against the fixed function path.

## Prevention

**Recommendations to avoid similar bugs:**
- Keep account-type branches as explicit early-return blocks, then put shared fallback code at the outer indentation level.
- Add body-lookup tests whenever a list endpoint filters on a field that may require a detail fetch.

## Related

- Cockpit Tools "测试 OTP" outlookEmail integration.
