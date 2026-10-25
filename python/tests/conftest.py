import sys
from pathlib import Path

# Make the src layout importable when running pytest from anywhere.
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
