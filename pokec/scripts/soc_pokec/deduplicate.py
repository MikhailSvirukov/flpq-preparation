#!/usr/bin/env python3
"""Remove parallel edges from a soc-Pokec edge list.

The source format is one directed relation per line, ``<tail><TAB><head>``,
with 1-based user ids (see https://snap.stanford.edu/data/soc-Pokec.html).
This script removes repeated (parallel) edges between the same ordered pair
of vertices and keeps the ids and the file format unchanged, so the result
can be fed to ``convert.py``.

Orientation is preserved: ``(u, v)`` and ``(v, u)`` are two different
directed relations and both are kept. Self-loops are kept unless repeated.

Duplicates are removed with an external merge sort: the input is split into
sorted chunks, then the chunks are merged with ``heapq.merge``. Memory use is
bounded by ``--chunk-size`` and does not depend on the input size.

Usage
-----
::

    python scripts/soc_pokec/deduplicate.py soc-pokec-relationships.txt.gz
    # -> soc-pokec-relationships.dedup.txt

    python scripts/soc_pokec/deduplicate.py soc-pokec-relationships.txt.gz \\
        -o dedup.txt.gz --duplicates duplicates.csv
    python scripts/soc_pokec/convert.py dedup.txt.gz

The output is sorted by ``(tail, head)``. With ``--duplicates FILE`` the
removed edges are also written to ``FILE`` as ``label,start,end`` rows, with
the label given by ``--label`` (default ``friend``) and the total number of
removed edges in the ``# duplicates:`` header line.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import heapq
import os
import pathlib
import shutil
import sys
import tempfile
from typing import Iterator

try:  # run as a module (python -m scripts.soc_pokec.deduplicate)
    from .common import Edge, iter_edges
except ImportError:  # run as a script (python scripts/soc_pokec/deduplicate.py)
    from common import Edge, iter_edges

#: Node ids are packed into one integer as ``tail << 32 | head``.
_SHIFT = 32
_MAX_ID = (1 << _SHIFT) - 1


def _pack(tail: int, head: int) -> int:
    if tail > _MAX_ID or head > _MAX_ID:
        raise ValueError(
            f"node id exceeds {_MAX_ID}: ({tail}, {head})"
        )
    return (tail << _SHIFT) | head


def _unpack(key: int) -> Edge:
    return key >> _SHIFT, key & _MAX_ID


def _write_chunk(keys: list[int], directory: pathlib.Path, index: int) -> pathlib.Path:
    """Sort one in-memory chunk and write it.

    Duplicates are kept: dropping them here would hide within-chunk
    duplicates from the global merge and from the duplicates report.
    """
    keys.sort()
    path = directory / f"chunk-{index:05d}.txt"
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for key in keys:
            tail, head = _unpack(key)
            stream.write(f"{tail}\t{head}\n")
    return path


def _read_chunk(path: pathlib.Path) -> Iterator[int]:
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            tail, head = line.split()
            yield (int(tail) << _SHIFT) | int(head)


def _open_output(path: pathlib.Path, *, gzipped: bool):
    if gzipped:
        return gzip.open(path, "wt", encoding="utf-8", newline="\n")
    return path.open("w", encoding="utf-8", newline="\n")


def deduplicate(
    source: pathlib.Path,
    destination: pathlib.Path,
    *,
    chunk_size: int = 2_000_000,
    temp_dir: pathlib.Path | None = None,
    label: str = "friend",
    duplicates: pathlib.Path | None = None,
) -> tuple[int, int]:
    """Write ``source`` without parallel edges; return ``(input, unique)``.

    Parameters
    ----------
    source : Path
        soc-Pokec edge list (plain text or ``.gz``).
    destination : Path
        Output edge list. ``.gz`` selects gzip compression.
    chunk_size : int, default 2_000_000
        Number of edges kept in memory per sorted chunk.
    temp_dir : Path, optional
        Where to put the chunk files. Defaults to the output directory.
    label : str, default "friend"
        Edge label written to the duplicates report.
    duplicates : Path, optional
        Report file for the removed edges: ``label,start,end`` rows prefixed
        with a ``# duplicates: <count>`` header. ``.gz`` selects gzip.
    """
    if chunk_size <= 0:
        raise ValueError("--chunk-size must be positive")
    if duplicates is not None:
        if duplicates.resolve() == destination.resolve():
            raise ValueError("--duplicates must differ from --output")
        if duplicates.resolve() == source.resolve():
            raise ValueError("--duplicates must differ from the source file")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = temp_dir if temp_dir is not None else destination.parent
    staging.mkdir(parents=True, exist_ok=True)

    chunks: list[pathlib.Path] = []
    buffer: list[int] = []
    num_input = 0
    with tempfile.TemporaryDirectory(prefix=".dedup-", dir=staging) as temporary:
        chunk_dir = pathlib.Path(temporary)
        for tail, head in iter_edges(source):
            buffer.append(_pack(tail, head))
            num_input += 1
            if len(buffer) >= chunk_size:
                chunks.append(_write_chunk(buffer, chunk_dir, len(chunks)))
                buffer = []
        if buffer:
            chunks.append(_write_chunk(buffer, chunk_dir, len(chunks)))

        duplicate_rows = chunk_dir / "duplicates.csv"
        duplicate_stream = (
            duplicate_rows.open("w", encoding="utf-8", newline="")
            if duplicates is not None
            else None
        )
        duplicate_writer = (
            csv.writer(duplicate_stream, lineterminator="\n")
            if duplicate_stream is not None
            else None
        )
        temporary_output = destination.with_name(destination.name + ".tmp")
        try:
            with _open_output(
                temporary_output, gzipped=destination.suffix == ".gz"
            ) as stream:
                previous: int | None = None
                num_unique = 0
                for key in heapq.merge(*(_read_chunk(path) for path in chunks)):
                    if key == previous:
                        if duplicate_writer is not None:
                            duplicate_writer.writerow([label, *_unpack(key)])
                        continue
                    previous = key
                    tail, head = _unpack(key)
                    stream.write(f"{tail}\t{head}\n")
                    num_unique += 1
            if duplicate_stream is not None:
                duplicate_stream.close()
                duplicate_stream = None
            if duplicates is not None:
                _write_duplicate_report(
                    duplicates,
                    label=label,
                    count=num_input - num_unique,
                    rows=duplicate_rows,
                )
            os.replace(temporary_output, destination)
        except BaseException:
            temporary_output.unlink(missing_ok=True)
            raise
        finally:
            if duplicate_stream is not None:
                duplicate_stream.close()

    print(f"source:      {source}")
    print(f"output:      {destination}")
    print(f"input edges: {num_input}")
    print(f"unique:      {num_unique}")
    print(f"duplicates:  {num_input - num_unique}")
    if duplicates is not None:
        print(f"report:      {duplicates}")
    return num_input, num_unique


def _write_duplicate_report(
    path: pathlib.Path,
    *,
    label: str,
    count: int,
    rows: pathlib.Path,
) -> None:
    """Write the removed edges with a ``# duplicates: <count>`` header."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        with _open_output(temporary, gzipped=path.suffix == ".gz") as stream:
            stream.write(f"# label: {label}\n")
            stream.write(f"# duplicates: {count}\n")
            stream.write("label,start,end\n")
            with rows.open("r", encoding="utf-8") as source:
                shutil.copyfileobj(source, stream)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Remove parallel edges from a soc-Pokec edge list"
    )
    parser.add_argument(
        "source", type=pathlib.Path, help="soc-pokec-relationships.txt[.gz]"
    )
    parser.add_argument(
        "-o",
        "--output",
        type=pathlib.Path,
        default=None,
        help="output edge list, .gz for compression "
        "(default: <source stem>.dedup.txt)",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=2_000_000,
        help="edges per sorted chunk (default: 2000000)",
    )
    parser.add_argument(
        "--temp-dir",
        type=pathlib.Path,
        default=None,
        help="directory for temporary chunks (default: output directory)",
    )
    parser.add_argument(
        "--label",
        default="friend",
        help="edge label written to the duplicates report (default: friend)",
    )
    parser.add_argument(
        "--duplicates",
        type=pathlib.Path,
        default=None,
        metavar="FILE",
        help="write the removed edges to FILE as label,start,end rows "
        "with a '# duplicates: <count>' header (.gz supported)",
    )
    args = parser.parse_args(argv)

    if not args.source.is_file():
        parser.error(f"{args.source} is not a file")
    output = args.output
    if output is None:
        stem = args.source.name
        for suffix in (".gz", ".txt"):
            if stem.endswith(suffix):
                stem = stem[: -len(suffix)]
        output = args.source.with_name(f"{stem}.dedup.txt")
    try:
        deduplicate(
            args.source,
            output,
            chunk_size=args.chunk_size,
            temp_dir=args.temp_dir,
            label=args.label,
            duplicates=args.duplicates,
        )
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
