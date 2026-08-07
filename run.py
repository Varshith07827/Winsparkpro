"""Launcher entry point.

Equivalent to `python -m wadam.ui`, kept as a plain script so a packager
(PyInstaller and friends) has a single file to point at.
"""

import sys

from wadam.ui.app import main

if __name__ == "__main__":
    sys.exit(main())
