"""Register the current DVC outputs as a new model version. Owner action.

    uv run python scripts/register.py

Refuses on a dirty git tree or stale DVC outputs. Prints the new version.
"""

import os
import sys

from tripduration.registry import RegistryError, register

if __name__ == "__main__":
    uri = os.environ.get("MLFLOW_TRACKING_URI", "http://localhost:5001")
    try:
        v = register(uri)
    except RegistryError as e:
        print(f"refused: {e}", file=sys.stderr)
        sys.exit(2)
    print(f"registered nyc-taxi-trip-duration version {v} at {uri}")
