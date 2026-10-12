import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

# Каждый запуск тестов получает собственную базу.
# Не используем и не удаляем /tmp/test_planner.db.
TEST_DB_DIR = tempfile.mkdtemp(prefix="planner-pytest-")
TEST_DB_PATH = str(Path(TEST_DB_DIR) / "planner-test.db")

os.environ["DB_PATH"] = TEST_DB_PATH
os.environ["SECRET_KEY"] = "test-only-secret-key"
print(f"\nИзолированная тестовая база: {TEST_DB_PATH}")
