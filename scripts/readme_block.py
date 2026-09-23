"""Print the README's fresh-machine block, so a workflow can run it verbatim.

    uv run python scripts/readme_block.py fresh-machine > fresh.sh

The block sits between `<!-- NAME:start -->` and `<!-- NAME:end -->` and is
a single fenced bash block. Comments after `#` are kept (bash ignores them);
nothing is rewritten, so what runs is exactly what a reader copies.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

README = Path(__file__).resolve().parents[1] / "README.md"


def block(name: str, text: str) -> str:
    m = re.search(
        rf"<!-- {name}:start -->\s*```bash\n(.*?)```\s*<!-- {name}:end -->", text, re.S
    )
    if not m:
        raise SystemExit(f"README has no fenced bash block marked {name}")
    return m.group(1)


if __name__ == "__main__":
    sys.stdout.write(block(sys.argv[1], README.read_text()))
