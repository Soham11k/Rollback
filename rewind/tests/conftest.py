import os
import tempfile
from pathlib import Path

# Must run before server.py is imported.
_data = Path(tempfile.mkdtemp(prefix="rewind-test-data-"))
os.environ.setdefault("REWIND_DATA", str(_data))
os.environ.setdefault("OLLAMA_URL", "http://127.0.0.1:9")
