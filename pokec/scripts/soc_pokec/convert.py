#!/usr/bin/env python3
"""Convert the SNAP soc-Pokec edge list to FLPQ_Data MatrixMarket format.

Source
------
https://snap.stanford.edu/data/soc-Pokec.html

    L. Takac, M. Zabovsky. Data Analysis in Public Social Networks.
    International Scientific Conference & International Workshop
    "Present Day Trends of Innovations", May 2012, Lomza, Poland.

Input
-----
``soc-pokec-relationships.txt`` (optionally gzip-compressed, e.g. the
distributed ``soc-pokec-relationships.txt.gz``): one directed friendship
relation per line, two integer user ids separated by a tab. The ids are
1-based; blank lines and ``#`` comments are ignored.

Output
------
A Boolean MatrixMarket pattern matrix in the FLPQ_Data format::

    %%MatrixMarket matrix coordinate pattern general
    %%GraphBLAS type bool
    <num_nodes> <num_nodes> <num_edges>
    <tail> <head>
    ...

Node ids are shifted to 0-based (the minimum id in the file becomes 0 by
default), so the matrix is ready to be stored as ``graph/<label>.mtx`` in a
FLPQ_Data archive. The input is read in two streaming passes, so memory use
does not depend on the number of edges.

Usage
-----
::

    python scripts/soc_pokec/convert.py soc-pokec-relationships.txt.gz
    python scripts/soc_pokec/convert.py soc-pokec-relationships.txt.gz \\
        -o build/soc-pokec/graph --label friend
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys

try:  # run as a module (python -m scripts.soc_pokec.convert)
    from .common import iter_edges
except ImportError:  # run as a script (python scripts/soc_pokec/convert.py)
    from common import iter_edges

HEADER = (
    "%%MatrixMarket matrix coordinate pattern general",
    "%%GraphBLAS type bool",
)


def scan(path: pathlib.Path) -> tuple[int, int, int, int]:
    """Return ``(min_id, max_id, num_edges, num_self_loops)``."""
    min_id = max_id = None
    num_edges = 0
    num_self_loops = 0
    for tail, head in iter_edges(path):
        num_edges += 1
        if min_id is None or tail < min_id:
            min_id = tail
        if min_id is None or head < min_id:
            min_id = head
        if max_id is None or tail > max_id:
            max_id = tail
        if max_id is None or head > max_id:
            max_id = head
        if tail == head:
            num_self_loops += 1
    if min_id is None or max_id is None:
        raise ValueError(f"{path}: no edges found")
    return min_id, max_id, num_edges, num_self_loops


def convert(
    source: pathlib.Path,
    destination: pathlib.Path,
    *,
    base: int | None = None,
    num_nodes: int | None = None,
) -> tuple[int, int]:
    """Convert ``source`` to ``destination``; return ``(num_nodes, num_edges)``.

    Parameters
    ----------
    source : Path
        soc-Pokec edge list (plain text or ``.gz``).
    destination : Path
        Destination ``.mtx`` file.
    base : int, optional
        Value subtracted from every id. Defaults to the minimum id in the
        file, which turns 1-based ids into 0-based ones.
    num_nodes : int, optional
        Matrix dimension. Defaults to ``max_id - base + 1``.
    """
    min_id, max_id, num_edges, num_self_loops = scan(source)
    offset = min_id if base is None else base
    if offset > min_id:
        raise ValueError(
            f"--base {offset} is greater than the minimum id {min_id} in the file"
        )
    declared_nodes = max_id - offset + 1 if num_nodes is None else num_nodes
    actual_nodes = max_id - offset + 1
    if declared_nodes < actual_nodes:
        raise ValueError(
            f"--num-nodes {declared_nodes} is smaller than the required "
            f"{actual_nodes} (ids {min_id}..{max_id} shifted by {offset})"
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(f"{HEADER[0]}\n")
            stream.write(f"{HEADER[1]}\n")
            stream.write(f"{declared_nodes} {declared_nodes} {num_edges}\n")
            for tail, head in iter_edges(source):
                stream.write(f"{tail - offset} {head - offset}\n")
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise

    print(f"source:      {source}")
    print(f"output:      {destination}")
    print(f"matrix:      {declared_nodes} x {declared_nodes}")
    print(f"ids:         {min_id}..{max_id} -> {min_id - offset}..{max_id - offset}")
    print(f"edges:       {num_edges}")
    print(f"self-loops:  {num_self_loops}")
    return declared_nodes, num_edges


def _destination(raw: str, label: str) -> pathlib.Path:
    """Interpret ``--output`` as a file (``.mtx``) or a directory.

    Anything without an ``.mtx`` suffix is a directory: ``-o graph`` writes
    ``graph/<label>.mtx``. Pass ``-o graph/<label>.mtx`` to write one file.
    """
    path = pathlib.Path(raw)
    as_directory = (
        path.suffix != ".mtx" or raw.endswith(("/", os.sep)) or path.is_dir()
    )
    if as_directory:
        if path.exists() and not path.is_dir():
            raise ValueError(
                f"{path} exists and is a file, not a directory; remove it or "
                "pass an explicit .mtx path"
            )
        return path / f"{label}.mtx"
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert the SNAP soc-Pokec edge list to FLPQ_Data .mtx"
    )
    parser.add_argument("source", type=pathlib.Path, help="soc-pokec-relationships.txt[.gz]")
    parser.add_argument(
        "-o",
        "--output",
        default="soc-pokec.mtx",
        help="output .mtx file, or a directory (default: soc-pokec.mtx)",
    )
    parser.add_argument(
        "--label",
        default="friend",
        help="edge label used when --output is a directory (default: friend)",
    )
    parser.add_argument(
        "--base",
        type=int,
        default=None,
        help="subtract this from every id (default: the minimum id in the file)",
    )
    parser.add_argument(
        "--num-nodes",
        type=int,
        default=None,
        help="matrix dimension (default: max id - base + 1)",
    )
    args = parser.parse_args(argv)

    if not args.source.is_file():
        parser.error(f"{args.source} is not a file")
    try:
        convert(
            args.source,
            _destination(args.output, args.label),
            base=args.base,
            num_nodes=args.num_nodes,
        )
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
