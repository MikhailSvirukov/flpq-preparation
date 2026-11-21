"""Shared input/output helpers for the soc-Pokec scripts."""

from __future__ import annotations

import gzip
import pathlib
from typing import Iterator

Edge = tuple[int, int]


def open_text(path: pathlib.Path):
    """Open a plain or gzip-compressed text file (detected by magic bytes)."""
    with path.open("rb") as probe:
        gzipped = probe.read(2) == b"\x1f\x8b"
    if gzipped:
        return gzip.open(path, "rt", encoding="utf-8")
    return path.open("r", encoding="utf-8")


def iter_edges(path: pathlib.Path) -> Iterator[Edge]:
    """Yield ``(tail, head)`` pairs from a soc-Pokec edge list.

    Blank lines and lines starting with ``#`` are skipped. Each remaining
    line must contain exactly two non-negative integers separated by
    whitespace.
    """
    with open_text(path) as stream:
        for number, line in enumerate(stream, start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            parts = stripped.split()
            if len(parts) != 2:
                raise ValueError(
                    f"{path}:{number}: expected two ids separated by whitespace, "
                    f"got {stripped!r}"
                )
            try:
                tail, head = int(parts[0]), int(parts[1])
            except ValueError as error:
                raise ValueError(
                    f"{path}:{number}: ids must be integers, got {stripped!r}"
                ) from error
            if tail < 0 or head < 0:
                raise ValueError(f"{path}:{number}: negative node id")
            yield tail, head
