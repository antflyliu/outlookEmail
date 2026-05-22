import importlib
import os
import tempfile
import unittest


os.environ.setdefault("SECRET_KEY", "test-secret-key")
if "DATABASE_PATH" not in os.environ:
    _temp_dir = tempfile.mkdtemp(prefix="outlookEmail-error-tests-")
    os.environ["DATABASE_PATH"] = os.path.join(_temp_dir, "test.db")

web_outlook_app = importlib.import_module("web_outlook_app")


class ErrorHandlingTests(unittest.TestCase):
    def setUp(self):
        self.app = web_outlook_app.app
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def test_unknown_res_events_api_returns_404_not_500(self):
        response = self.client.get("/api/res-events")

        self.assertEqual(response.status_code, 404)
        payload = response.get_json()
        self.assertFalse(payload["success"])
        self.assertIn("not found", payload["error"].lower())


if __name__ == "__main__":
    unittest.main()
