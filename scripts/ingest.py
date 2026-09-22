"""CLI: download and normalise one TLC month, or the zone lookup.

    uv run python scripts/ingest.py --service yellow --month 2024-07
    uv run python scripts/ingest.py --zones

Exit 0 on success and when the month is not published yet (404). Any other
failure raises and exits non-zero.
"""

import logging
import sys

from tripduration.ingest import main

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    sys.exit(main())
