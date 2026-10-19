"""``python3 -m orchestrator``: run the deterministic, network-free demonstration."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Sequence

from .examples import collect_demo


def main(argv: Sequence[str] | None = None) -> int:
    """Print the demo summary; exit status 0 when every demonstrated feature worked."""
    result = asyncio.run(collect_demo())
    print(result.text)
    return 0 if all(result.checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
