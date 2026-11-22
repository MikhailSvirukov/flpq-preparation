#!/usr/bin/env python3
"""Convert YAGO2s simple Turtle to the FLPQ_Data MatrixMarket layout.

Input
-----
The ``yago-2.5.3-turtle-simple.ttl`` export from
https://yago-knowledge.org/downloads/yago-2s (the ``.7z`` must be extracted
first): one RDF triple per line, tab-separated, with ``@base``/``@prefix``
directives at the top.

Output
------
A directory (by default ``graph/``) with one Boolean MatrixMarket file per
predicate::

    graph/
    ├── linksTo.mtx
    ├── livesIn.mtx
    ├── isLocatedIn.mtx
    └── ...

The transformation is:

* **RDF/RDFS/OWL/SKOS predicates are dropped by default** (``rdf:type``,
  ``rdfs:subClassOf``, ``rdfs:label``, ``owl:sameAs``, ``skos:prefLabel``,
  ...): the result is a graph of real relations, not an RDF/OWL graph. Use
  ``--keep-schema`` to keep them;
* every remaining RDF term (subject/object, including literals) becomes a
  graph node;
* every remaining predicate becomes an edge label;
* **prefixes are removed from labels**: the predicate IRI is expanded and
  only its local name is kept (``rdfs:subClassOf`` -> ``subClassOf``,
  ``owl:sameAs`` -> ``sameAs``, ``<linksTo>`` -> ``linksTo``);
* node IRIs are expanded to full IRIs for identity (they are internal keys —
  the output matrices contain only integer ids);
* duplicate triples (parallel edges of one label between the same pair of
  nodes) are removed;
* node ids are contiguous, 0-based, assigned in the order of the sorted
  unique triples.

The input is processed in two streaming stages: normalise into sorted chunks,
then merge the chunks and write one matrix per label. Memory is bounded by
``--chunk-size`` plus the ``term -> id`` table (one entry per unique RDF term),
not by the number of triples.

Usage
-----
::

    7z e -so yago-2.5.3-turtle-simple.7z > yago-2.5.3-turtle-simple.ttl
    python3 yago/scripts/yago2s/convert.py yago-2.5.3-turtle-simple.ttl \\
        -o build/yago/graph
"""

from __future__ import annotations

import argparse
import array
import gzip
import hashlib
import heapq
import pathlib
import re
import shutil
import sys
import tempfile
import urllib.parse
from typing import Iterator

HEADER = (
    "%%MatrixMarket matrix coordinate pattern general",
    "%%GraphBLAS type bool",
)

_PREFIX_RE = re.compile(
    r"^(?:@prefix|PREFIX)\s+([A-Za-z_][A-Za-z0-9_.-]*):\s*<([^>]*)>\s*\.?\s*$"
)
_BASE_RE = re.compile(r"^(?:@base|BASE)\s*<([^>]*)>\s*\.?\s*$")
_ABSOLUTE_IRI_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")

#: Predicate namespaces dropped by default: the graph keeps only real
#: relations, not RDF/OWL/SKOS schema and metadata edges.
SCHEMA_NAMESPACES = (
    "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "http://www.w3.org/2000/01/rdf-schema#",
    "http://www.w3.org/2002/07/owl#",
    "http://www.w3.org/2004/02/skos/core#",
)

Triple = tuple[str, str, str]  # (label, subject, object)


def open_text(path: pathlib.Path):
    """Open plain or gzip-compressed text (detected by magic bytes)."""
    with path.open("rb") as probe:
        gzipped = probe.read(2) == b"\x1f\x8b"
    if gzipped:
        return gzip.open(path, "rt", encoding="utf-8")
    return path.open("r", encoding="utf-8")


def _expand_iri(iri: str, base: str) -> str:
    if _ABSOLUTE_IRI_RE.match(iri):
        return iri
    return urllib.parse.urljoin(base, iri)


def _expand_term(token: str, base: str, prefixes: dict[str, str]) -> str:
    """Return the identity key of an RDF term (IRI, literal, or blank node)."""
    if token.startswith("<"):
        if not token.endswith(">"):
            raise ValueError(f"malformed IRI {token!r}")
        return _expand_iri(token[1:-1], base)
    if token.startswith("_:"):
        return token
    if token.startswith('"') or token.startswith("'"):
        # Literals stay opaque nodes; their lexical form is the identity.
        return token
    if ":" in token:
        prefix, local = token.split(":", 1)
        if prefix in prefixes:
            return prefixes[prefix] + local
    raise ValueError(f"cannot expand RDF term {token!r}")


def _local_name(iri: str) -> str:
    if "#" in iri:
        name = iri.rsplit("#", 1)[1]
    else:
        name = iri.rstrip("/").rsplit("/", 1)[-1]
    if not name:
        name = iri
    return urllib.parse.unquote(name)


def _label_from_iri(iri: str, predicate: str) -> str:
    """Keep only the local name of an expanded predicate IRI."""
    label = _local_name(iri)
    # Keep matrix file names portable; YAGO labels are normally clean already.
    label = re.sub(r"[^A-Za-z0-9_.-]", "_", label)
    if not label:
        raise ValueError(f"empty predicate label for {predicate!r}")
    if label.endswith("_r"):
        raise ValueError(
            f"predicate {predicate!r} maps to the reserved reversed label "
            f"{label!r}"
        )
    if (
        "bar" in label.replace("_", " ").split()
        or "rev" in label.replace("_", " ").split()
    ):
        raise ValueError(
            f"predicate {predicate!r} maps to the forbidden label {label!r}"
        )
    return label


def _is_schema_predicate(iri: str) -> bool:
    return iri.startswith(SCHEMA_NAMESPACES)


def iter_triples(
    path: pathlib.Path,
    *,
    drop_schema: bool = True,
    stats: dict[str, int] | None = None,
) -> Iterator[Triple]:
    """Yield ``(label, subject, object)`` with expanded IRIs.

    With ``drop_schema=True`` (the default), triples whose predicate belongs
    to the RDF/RDFS/OWL/SKOS namespaces are skipped: the graph keeps only
    real relations. ``stats["dropped"]`` counts the skipped triples.
    """
    base = "http://yago-knowledge.org/resource/"
    prefixes: dict[str, str] = {}
    labels: dict[str, str] = {}
    label_owner: dict[str, str] = {}
    with open_text(path) as stream:
        for number, line in enumerate(stream, start=1):
            line = line.rstrip("\r\n")
            if not line.strip():
                continue
            if line.lstrip().startswith("#"):
                continue
            stripped = line.strip()
            if (
                stripped.startswith("@")
                or stripped.upper().startswith("PREFIX")
                or stripped.upper().startswith("BASE")
            ):
                prefix_match = _PREFIX_RE.match(stripped)
                if prefix_match:
                    prefixes[prefix_match.group(1)] = prefix_match.group(2)
                    continue
                base_match = _BASE_RE.match(stripped)
                if base_match:
                    base = base_match.group(1)
                    continue
                raise ValueError(f"{path}:{number}: cannot parse directive {line!r}")
            parts = line.split("\t")
            if len(parts) != 4 or not parts[3].strip().startswith("."):
                raise ValueError(
                    f"{path}:{number}: expected four tab-separated fields "
                    f"(subject, predicate, object, '.'), got {len(parts)}"
                )
            predicate = parts[1]
            predicate_iri = _expand_term(predicate, base, prefixes)
            if drop_schema and _is_schema_predicate(predicate_iri):
                if stats is not None:
                    stats["dropped"] = stats.get("dropped", 0) + 1
                continue
            subject = _expand_term(parts[0], base, prefixes)
            label = labels.get(predicate_iri)
            if label is None:
                label = _label_from_iri(predicate_iri, predicate)
                owner = label_owner.setdefault(label, predicate)
                if owner != predicate:
                    raise ValueError(
                        f"{path}:{number}: predicates {owner!r} and "
                        f"{predicate!r} both map to label {label!r}"
                    )
                labels[predicate_iri] = label
            obj = _expand_term(parts[2], base, prefixes)
            yield label, subject, obj


def _write_chunk(triples: list[Triple], directory: pathlib.Path, index: int) -> pathlib.Path:
    triples.sort()
    path = directory / f"chunk-{index:05d}.tsv"
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for label, subject, obj in triples:
            stream.write(f"{label}\t{subject}\t{obj}\n")
    return path


def _read_chunk(path: pathlib.Path):
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            label, subject, obj = line.rstrip("\n").split("\t")
            yield label, subject, obj


class TermTable:
    """Compact ``RDF term -> contiguous id`` table.

    Storing tens of millions of full IRIs in a Python ``dict`` costs many
    gigabytes. This table keeps a 128-bit BLAKE2b digest of each term in two
    ``uint64`` arrays and resolves collisions with open addressing. A 128-bit
    digest collision over YAGO's ~4e7 terms has negligible probability.
    """

    _LOAD_LIMIT = 0.85

    def __init__(self, expected_terms: int) -> None:
        if expected_terms <= 0:
            raise ValueError("--expected-terms must be positive")
        capacity = 16
        while capacity * self._LOAD_LIMIT < expected_terms:
            capacity <<= 1
        self.capacity = capacity
        self.mask = capacity - 1
        self.first = array.array("Q", [0]) * capacity
        self.second = array.array("Q", [0]) * capacity
        self.ids = array.array("I", [0]) * capacity
        self.size = 0

    def id(self, term: str) -> int:
        digest = hashlib.blake2b(term.encode("utf-8"), digest_size=16).digest()
        first = int.from_bytes(digest[:8], "little")
        second = int.from_bytes(digest[8:], "little")
        index = first & self.mask
        while True:
            value = self.ids[index]
            if value == 0:
                if self.size >= self.capacity * self._LOAD_LIMIT:
                    raise ValueError(
                        "term table is full; rerun with a larger --expected-terms"
                    )
                self.first[index] = first
                self.second[index] = second
                result = self.size
                self.ids[index] = result + 1
                self.size += 1
                return result
            if self.first[index] == first and self.second[index] == second:
                return value - 1
            index = (index + 1) & self.mask


def convert(
    source: pathlib.Path,
    output: pathlib.Path,
    *,
    chunk_size: int = 1_000_000,
    temp_dir: pathlib.Path | None = None,
    expected_terms: int = 50_000_000,
    drop_schema: bool = True,
) -> tuple[int, int, int]:
    """Convert ``source`` to ``output``; return ``(nodes, labels, edges)``."""
    if chunk_size <= 0:
        raise ValueError("--chunk-size must be positive")
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError(f"{output} is not empty; remove it or choose another directory")

    staging = temp_dir if temp_dir is not None else output.parent
    staging.mkdir(parents=True, exist_ok=True)

    chunks: list[pathlib.Path] = []
    buffer: list[Triple] = []
    num_input = 0
    stats: dict[str, int] = {}
    with tempfile.TemporaryDirectory(prefix=".yago-", dir=staging) as temporary:
        chunk_dir = pathlib.Path(temporary)
        for triple in iter_triples(source, drop_schema=drop_schema, stats=stats):
            buffer.append(triple)
            num_input += 1
            if len(buffer) >= chunk_size:
                chunks.append(_write_chunk(buffer, chunk_dir, len(chunks)))
                buffer = []
                print(f"\rread: {num_input:,} triples", end="", file=sys.stderr)
        if buffer:
            chunks.append(_write_chunk(buffer, chunk_dir, len(chunks)))
            buffer.clear()
        print(f"\rread: {num_input:,} triples", file=sys.stderr)

        bodies: dict[str, pathlib.Path] = {}
        handles: dict[str, object] = {}
        counts: dict[str, int] = {}
        terms = TermTable(expected_terms)

        previous: Triple | None = None
        num_unique = 0
        try:
            for label, subject, obj in heapq.merge(
                *(_read_chunk(path) for path in chunks)
            ):
                triple = (label, subject, obj)
                if triple == previous:
                    continue
                previous = triple
                num_unique += 1
                body = bodies.get(label)
                if body is None:
                    body = chunk_dir / f"body-{len(bodies):05d}.tsv"
                    bodies[label] = body
                    handles[label] = body.open("w", encoding="utf-8", newline="\n")
                    counts[label] = 0
                tail = terms.id(subject)
                head = terms.id(obj)
                handles[label].write(f"{tail} {head}\n")  # type: ignore[union-attr]
                counts[label] += 1
                if num_unique % 10_000_000 == 0:
                    print(
                        f"\runique: {num_unique:,}  nodes: {terms.size:,}",
                        end="",
                        file=sys.stderr,
                    )
        finally:
            for handle in handles.values():
                handle.close()  # type: ignore[union-attr]
            print(file=sys.stderr)

        num_nodes = terms.size
        if num_nodes == 0:
            raise ValueError(f"{source}: no triples found")

        for label in sorted(bodies):
            destination = output / f"{label}.mtx"
            with destination.open("w", encoding="utf-8", newline="\n") as stream:
                stream.write(f"{HEADER[0]}\n")
                stream.write(f"{HEADER[1]}\n")
                stream.write(f"{num_nodes} {num_nodes} {counts[label]}\n")
                with bodies[label].open("r", encoding="utf-8") as body:
                    shutil.copyfileobj(body, stream)

    print(f"source:        {source}")
    print(f"output:        {output}")
    print(f"triples read:  {num_input}")
    print(f"schema dropped:{stats.get('dropped', 0)}")
    print(f"unique edges:  {sum(counts.values())}")
    print(f"duplicates:    {num_input - num_unique}")
    print(f"nodes:         {num_nodes}")
    print(f"labels:        {len(bodies)}")
    return num_nodes, len(bodies), sum(counts.values())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert YAGO2s simple Turtle to FLPQ_Data MatrixMarket"
    )
    parser.add_argument("source", type=pathlib.Path, help="YAGO .ttl[.gz] file")
    parser.add_argument(
        "-o",
        "--output",
        type=pathlib.Path,
        default=pathlib.Path("graph"),
        help="output graph directory (default: graph)",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=1_000_000,
        help="triples per sorted chunk (default: 1000000)",
    )
    parser.add_argument(
        "--temp-dir",
        type=pathlib.Path,
        default=None,
        help="directory for temporary chunks (default: output directory)",
    )
    parser.add_argument(
        "--expected-terms",
        type=int,
        default=50_000_000,
        help="expected number of unique RDF terms (default: 50000000); "
        "increase it if the converter reports that the term table is full",
    )
    parser.add_argument(
        "--keep-schema",
        action="store_true",
        help="keep RDF/RDFS/OWL/SKOS predicates; by default only real "
        "relations are converted",
    )
    args = parser.parse_args(argv)

    if not args.source.is_file():
        parser.error(f"{args.source} is not a file")
    try:
        convert(
            args.source,
            args.output,
            chunk_size=args.chunk_size,
            temp_dir=args.temp_dir,
            expected_terms=args.expected_terms,
            drop_schema=not args.keep_schema,
        )
    except (ValueError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
