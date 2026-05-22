# Bugfix Report: Unknown API 404 Converted To 500

## Summary

Requests for unknown API routes, such as `/api/res-events`, were converted from Flask routing 404 errors into application 500 responses.

## Impact

When a stale browser tab or another local frontend kept polling an endpoint that does not exist in OutlookEmail, the server logged the request as an unhandled exception and returned HTTP 500. This made normal unknown-route traffic look like an application startup failure.

## Root Cause

`outlook_web/segments/08_forwarding_scheduler_errors.py` registered a generic `@app.errorhandler(Exception)` handler. Werkzeug routing errors such as `NotFound` are `HTTPException` instances, but they also inherit from `Exception`, so the generic handler caught them, printed a traceback, and returned a 500 response.

## Fix

Added a dedicated `HTTPException` handler that preserves the original HTTP status code and returns the existing JSON error shape. The generic exception handler now also delegates `HTTPException` defensively.

## Regression Coverage

Added `tests/test_error_handling.py` with a focused regression for `/api/res-events`, asserting that an unknown API route returns 404 instead of 500.

## Verification

Commands run:

```powershell
python -m unittest discover -s tests -p test_error_handling.py -v
python -m unittest discover -s tests -p test_runtime_env.py -v
python -m unittest discover -s tests -p test_project_runtime.py -v
python -c "import os, tempfile; os.environ.setdefault('SECRET_KEY','test-secret-key'); os.environ['DATABASE_PATH']=tempfile.mkdtemp(prefix='outlookEmail-api-check-') + '/test.db'; import web_outlook_app; c=web_outlook_app.app.test_client(); r=c.get('/api/res-events'); print(r.status_code, r.get_json())"
```

Results:

- `test_error_handling.py`: 1 test passed.
- `test_runtime_env.py`: 1 test passed.
- `test_project_runtime.py`: 46 tests passed.
- Direct API check returned `404` with `{"success": false, ...}`.

## Remaining Notes

`/api/res-events` is not an OutlookEmail route. Seeing that request usually means a stale ResDownloader/ytdlp frontend page or browser tab is still running against the same host and port. After this fix, those stray requests are handled as normal 404s instead of being reported as server failures.
