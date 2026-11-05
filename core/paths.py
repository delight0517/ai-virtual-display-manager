from pathlib import Path
import sys

APP_ROOT = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
            else Path(__file__).resolve().parents[1])
APP_ID = "ai-virtual-display-manager"
APP_VERSION = "0.1.100"






