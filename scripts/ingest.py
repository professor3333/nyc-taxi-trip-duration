"""CLI: fetch TLC months byte-for-byte, the zone lookup, or verify raw md5s.

    uv run python scripts/ingest.py --service yellow --month 2024-10 2024-11
    uv run python scripts/ingest.py --zones
    uv run python scripts/ingest.py --verify

Exit 0 on success and when a month is not published yet (a 403/404 for a new,
recent month while the source still answers). Exit 1 on a source-access
failure (a 403/404 that cannot mean "not yet"). --verify exits 1 if any raw
file's md5 differs from its report. Any other failure raises and exits
non-zero. A refused download stays in data/quarantine/; data/raw/ is untouched.
"""

import logging
import sys

from tripduration.ingest import main

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    sys.exit(main())
