"""Make the src layout importable without requiring an editable install."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
