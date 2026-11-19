"""Put the package root on sys.path so `import bflite` works however pytest is started."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
