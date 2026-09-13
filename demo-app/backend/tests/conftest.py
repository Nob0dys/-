import os
import sys
import tempfile
from pathlib import Path


TEST_ROOT = Path(tempfile.mkdtemp(prefix="quote-api-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_ROOT / 'test.db'}"
os.environ["QUOTE_DATA_DIR"] = str(TEST_ROOT / "data")
os.environ["HISTORY_SEED_PATH"] = str(Path(__file__).resolve().parents[2] / "public" / "demo" / "history.json")
os.environ["QUOTE_RUN_INLINE_JOBS"] = "true"
os.environ["DEFAULT_ADMIN_PASSWORD"] = "admin123"
