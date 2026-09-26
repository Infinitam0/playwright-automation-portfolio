"""Console + file logging, UTF-8 safe.

Windows consoles default to cp1252, and business names on Maps are full of emoji
and non-Latin scripts. Without this, printing a harvested name raises
UnicodeEncodeError and kills a run that was otherwise working — the data is fine,
only the display is not. Reconfigure once, centrally, so no script has to care.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path


def setup(level: int = logging.INFO, log_file: Path | None = None) -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,
    )
    # Playwright's own chatter is noise at INFO.
    logging.getLogger("asyncio").setLevel(logging.WARNING)
