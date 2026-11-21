#!/usr/bin/env python3
"""Compute an RPQ answer matrix from MatrixMarket input.

Input
-----
* ``GRAPH`` — either a directory with one Boolean MatrixMarket file per edge
  label (the ``graph/`` directory of a FLPQ_Data archive) or a single
  ``<label>.mtx`` file. Labels are taken from the file names.
* ``QUERY`` — an ``.re`` file in pyformlang ``Regex`` syntax. This is the
  only query representation the tool reads.

Output
------
``results.mtx`` — a Boolean MatrixMarket pattern matrix with the graph's
dimensions. Entry ``(u, v)`` is present iff some path from ``u`` to ``v`` is
labelled by a word of the query language.

The matrix always covers **all** node pairs: an ``.re`` file defines only the
language, and FLPQ_Data has no place to store a fixed start node. A start
node from the original SPARQL query (if any) is not part of the RPQ format;
it is an experiment parameter and can be applied by filtering the finished
matrix.

The graph is interpreted with FLPQ_Data semantics: for every stored label
``L`` the reversed label ``L_r`` is derived automatically (it must not be
stored in the input).

Algorithm
---------
The query is converted to a DFA (pyformlang). Then, for every start node,
a BFS is run over the product graph ``graph node x DFA state``; a pair is
written when the DFA reaches a final state. Memory is O(N * |Q| + E), where
``N`` is the number of nodes, ``E`` the number of edges and ``|Q|`` the
number of DFA states; time is O(N * (N + E) * |Q|) in the worst case.

Dependencies: ``pyformlang`` (``pip install pyformlang``).

Usage
-----
::

    python scripts/rpq_results.py graph/ queries/rpq/3/query.re -o results.mtx
    python scripts/rpq_results.py friend.mtx query.re -o results.mtx
"""

from __future__ import annotations

import argparse
import array
import os
import pathlib
import shutil
import sys
from typing import Iterable, Iterator

try:
    from pyformlang.regular_expression import Regex
except ImportError as error:  # pragma: no cover - environment setup
    raise SystemExit(
        "error: pyformlang is required: pip install pyformlang "
        "(or pip install flpq-data)"
    ) from error

HEADER = (
    "%%MatrixMarket matrix coordinate pattern general",
    "%%GraphBLAS type bool",
)

#: An edge as ``(tail, label, head)``.
Edge = tuple[int, str, int]


def _check_header(path: pathlib.Path, stream) -> tuple[int, int, int]:
    first = stream.readline().rstrip("\r\n")
    second = stream.readline().rstrip("\r\n")
    if (first, second) != HEADER:
        raise ValueError(f"{path}: unexpected MatrixMarket header")
    try:
        rows, cols, declared = (int(value) for value in stream.readline().split())
    except ValueError as error:
        raise ValueError(f"{path}: malformed dimension line") from error
    if rows != cols:
        raise ValueError(f"{path}: the matrix must be square, got {rows}x{cols}")
    return rows, cols, declared


def iter_mtx(path: pathlib.Path, label: str) -> Iterator[Edge]:
    """Yield ``(tail, label, head)`` from one MatrixMarket label matrix."""
    with path.open("r", encoding="utf-8") as stream:
        rows, cols, declared = _check_header(path, stream)
        count = 0
        for number, line in enumerate(stream, start=4):
            if not line.strip():
                continue
            try:
                tail, head = (int(value) for value in line.split())
            except ValueError as error:
                raise ValueError(f"{path}:{number}: malformed entry") from error
            if not 0 <= tail < rows or not 0 <= head < cols:
                raise ValueError(
                    f"{path}:{number}: edge ({tail}, {head}) is outside "
                    f"the {rows}x{cols} matrix"
                )
            count += 1
            yield tail, label, head
    if count != declared:
        raise ValueError(
            f"{path}: declares {declared} entries but contains {count}"
        )


def graph_files(graph: pathlib.Path) -> list[pathlib.Path]:
    """Return the label matrices of ``GRAPH`` (a directory or one file)."""
    if graph.is_dir():
        files = sorted(graph.glob("*.mtx"))
        if not files:
            raise ValueError(f"{graph}: no .mtx files found")
        return files
    if graph.is_file():
        if graph.suffix != ".mtx":
            raise ValueError(f"{graph}: expected a .mtx file or a directory")
        return [graph]
    raise ValueError(f"{graph}: no such file or directory")


def graph_num_nodes(graph: pathlib.Path) -> int:
    """Read and validate the dimensions declared by every label matrix."""
    dimensions = set()
    for path in graph_files(graph):
        with path.open("r", encoding="utf-8") as stream:
            rows, _cols, _declared = _check_header(path, stream)
        dimensions.add(rows)
    if len(dimensions) != 1:
        raise ValueError(
            f"{graph}: all label matrices must declare the same dimensions "
            f"(found {sorted(dimensions)})"
        )
    return dimensions.pop()


def iter_graph_edges(graph: pathlib.Path) -> Iterator[Edge]:
    """Yield every stored edge of ``GRAPH`` with the label from its file name."""
    for path in graph_files(graph):
        yield from iter_mtx(path, path.stem)


def dfa_from_re(path: pathlib.Path):
    """Build the DFA of an ``.re`` query file."""
    if path.suffix != ".re":
        raise ValueError(f"{path}: expected an .re file")
    text = path.read_text(encoding="utf-8")
    if text != text.rstrip():
        print(
            f"warning: {path} has trailing whitespace; pyformlang treats it as "
            "part of the last symbol",
            file=sys.stderr,
        )
    nfa = Regex(text).to_epsilon_nfa()
    if nfa is None:
        raise ValueError(f"{path}: the regular expression is empty")
    return nfa.to_deterministic().minimize()


def dfa_data(dfa) -> tuple[list[dict[str, int]], set[int], int]:
    """Convert a pyformlang DFA to ``(transitions, finals, start)`` indices."""
    raw = dfa.to_dict()
    states = set(dfa.states)
    states.update(raw)
    for edges in raw.values():
        states.update(edges.values())
    ordered = sorted(states, key=lambda state: str(state.value))
    index = {state: number for number, state in enumerate(ordered)}

    transitions: list[dict[str, int]] = [dict() for _ in ordered]
    for source, edges in raw.items():
        for symbol, target in edges.items():
            transitions[index[source]][symbol.value] = index[target]

    finals = {index[state] for state in dfa.final_states}
    return transitions, finals, index[dfa.start_state]


def build_csr(
    graph: pathlib.Path, num_nodes: int, needed_labels: Iterable[str]
) -> tuple[array.array, array.array, array.array, int]:
    """Build a CSR adjacency for the labels the query can traverse.

    Stored labels are used as-is; for a needed label ``L_r`` the reversed
    edges of the stored ``L`` are added on the fly. Labels that occur in the
    query but not in the graph simply contribute no edges.
    """
    label_ids = {
        label: number for number, label in enumerate(sorted(set(needed_labels)))
    }

    def kept_edges() -> Iterator[tuple[int, int, int]]:
        for tail, label, head in iter_graph_edges(graph):
            if label in label_ids:
                yield tail, head, label_ids[label]
            reversed_label = f"{label}_r"
            if reversed_label in label_ids:
                yield head, tail, label_ids[reversed_label]

    degrees = [0] * num_nodes
    edge_count = 0
    for tail, _head, _label in kept_edges():
        degrees[tail] += 1
        edge_count += 1
    if edge_count > 0xFFFFFFFF:
        raise ValueError("the graph is too large for 32-bit CSR indices")

    indptr = array.array("I", [0]) * (num_nodes + 1)
    running = 0
    for node, degree in enumerate(degrees):
        indptr[node] = running
        running += degree
    indptr[num_nodes] = running

    heads = array.array("I", [0]) * edge_count
    labels = array.array("I", [0]) * edge_count
    cursor = array.array("I", indptr[:-1])
    for tail, head, label in kept_edges():
        position = cursor[tail]
        heads[position] = head
        labels[position] = label
        cursor[tail] = position + 1
    return indptr, heads, labels, edge_count


def compute(
    graph: pathlib.Path,
    query: pathlib.Path,
    output: pathlib.Path,
) -> tuple[int, int, int]:
    """Compute the answer matrix; return ``(nodes, states, results)``."""
    path = query
    dfa = dfa_from_re(path)
    transitions, finals, start_state = dfa_data(dfa)
    num_states = len(transitions)

    num_nodes = graph_num_nodes(graph)

    needed_labels = {label for state in transitions for label in state}
    label_ids = {
        label: number for number, label in enumerate(sorted(needed_labels))
    }
    if needed_labels:
        transitions = [
            {label_ids[label]: target for label, target in state.items()}
            for state in transitions
        ]
    indptr, heads, edge_labels, edge_count = build_csr(
        graph, num_nodes, needed_labels
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    body_path = output.with_name(output.name + ".body.tmp")
    final_path = output.with_name(output.name + ".tmp")

    visited = array.array("I", [0]) * (num_nodes * num_states)
    marked = array.array("I", [0]) * num_nodes
    generation = 0
    total = 0

    try:
        with body_path.open("w", encoding="utf-8", newline="\n") as body:
            for start in range(num_nodes):
                generation += 1
                if generation > 0xFFFFFFFF:  # pragma: no cover - 4e9 rows
                    visited = array.array("I", [0]) * (num_nodes * num_states)
                    marked = array.array("I", [0]) * num_nodes
                    generation = 1

                queue = array.array("I", [start * num_states + start_state])
                visited[start * num_states + start_state] = generation
                row: list[int] = []
                if start_state in finals:
                    marked[start] = generation
                    row.append(start)

                position = 0
                while position < len(queue):
                    code = queue[position]
                    position += 1
                    node, state = divmod(code, num_states)
                    delta = transitions[state]
                    if not delta:
                        continue
                    for edge in range(indptr[node], indptr[node + 1]):
                        target_state = delta.get(edge_labels[edge])
                        if target_state is None:
                            continue
                        target_node = heads[edge]
                        code = target_node * num_states + target_state
                        if visited[code] == generation:
                            continue
                        visited[code] = generation
                        queue.append(code)
                        if (
                            target_state in finals
                            and marked[target_node] != generation
                        ):
                            marked[target_node] = generation
                            row.append(target_node)

                row.sort()
                for target in row:
                    body.write(f"{start} {target}\n")
                total += len(row)

                step = max(1, num_nodes // 10)
                if (start + 1) % step == 0:
                    print(
                        f"\rrows: {start + 1}/{num_nodes}  results: {total}",
                        end="",
                        file=sys.stderr,
                        flush=True,
                    )
            print(file=sys.stderr)

        with final_path.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(f"{HEADER[0]}\n")
            stream.write(f"{HEADER[1]}\n")
            stream.write(f"{num_nodes} {num_nodes} {total}\n")
            with body_path.open("r", encoding="utf-8") as body:
                shutil.copyfileobj(body, stream)
        os.replace(final_path, output)
    except BaseException:
        body_path.unlink(missing_ok=True)
        final_path.unlink(missing_ok=True)
        raise
    body_path.unlink(missing_ok=True)

    print(f"query:        {path}")
    print(f"nodes:        {num_nodes}")
    print(f"states:       {num_states}")
    print(f"edge count:   {edge_count}")
    print(f"rows:         {num_nodes}")
    print(f"result pairs: {total}")
    print(f"output:       {output}")
    return num_nodes, num_states, total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compute an RPQ answer matrix in MatrixMarket format"
    )
    parser.add_argument(
        "graph",
        type=pathlib.Path,
        help="directory with <label>.mtx files, or a single <label>.mtx",
    )
    parser.add_argument(
        "query",
        type=pathlib.Path,
        help=".re query file (pyformlang Regex syntax)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=pathlib.Path,
        default=pathlib.Path("results.mtx"),
        help="output MatrixMarket file (default: results.mtx)",
    )
    args = parser.parse_args(argv)

    try:
        num_nodes = graph_num_nodes(args.graph)
        if num_nodes > 100_000:
            print(
                f"warning: computing all {num_nodes} rows; the answer matrix "
                "may be enormous.",
                file=sys.stderr,
            )
        compute(args.graph, args.query, args.output)
    except (ValueError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
