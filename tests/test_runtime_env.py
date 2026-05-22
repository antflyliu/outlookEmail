import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from outlook_web.runtime import load_local_env


class RuntimeEnvTests(unittest.TestCase):
    def test_load_local_env_reads_dotenv_without_overriding_process_env(self):
        with tempfile.TemporaryDirectory(prefix="outlookEmail-env-test-") as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "# comment",
                        "SECRET_KEY=from-file",
                        "EXISTING=from-file",
                        'QUOTED_VALUE="quoted value"',
                        "export EXPORTED_VALUE=yes",
                        "INLINE_COMMENT=value # ignored",
                    ]
                ),
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"EXISTING": "from-process"}, clear=True):
                load_local_env(env_path)

                self.assertEqual(os.environ["SECRET_KEY"], "from-file")
                self.assertEqual(os.environ["EXISTING"], "from-process")
                self.assertEqual(os.environ["QUOTED_VALUE"], "quoted value")
                self.assertEqual(os.environ["EXPORTED_VALUE"], "yes")
                self.assertEqual(os.environ["INLINE_COMMENT"], "value")


if __name__ == "__main__":
    unittest.main()
