"""bflite - a pure-Python stand-in for the Batfish config questions used for BGP / L3 / L2 extraction."""
from .datamodel import Interface
from .loader import load_snapshot, parse_config
from .session import Session

__all__ = ["Session", "Interface", "load_snapshot", "parse_config"]
__version__ = "0.1.0"
