"""Phase 0 smoke test: the package imports and CI can run pytest."""

import tripduration


def test_package_imports() -> None:
    assert tripduration.__version__ == "0.1.0"
