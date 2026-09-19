"""Stable isolated entrypoint used by Electron and the scoped LaunchAgent."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rapp_launchpad.cli import main

raise SystemExit(main())
