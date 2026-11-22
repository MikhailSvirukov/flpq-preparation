# soc-pokec

## What is this graph?

A directed social graph of the Pokec online social network (Slovakia). Nodes
are anonymized user ids; an edge `u -> v` means that user `u` has user `v`
as a friend. Friendships in Pokec are oriented.

## Source

SNAP — Pokec social network, <https://snap.stanford.edu/data/soc-Pokec.html>.

The source file is `soc-pokec-relationships.txt.gz` (one relation per line,
tab-separated 1-based user ids).

Citation:

> L. Takac, M. Zabovsky. Data Analysis in Public Social Networks.
> International Scientific Conference & International Workshop "Present Day
> Trends of Innovations", May 2012, Lomza, Poland.

The data was crawled during May 25–27, 2012 and extracted from the Wikipedia
dump of 2012-12-01.

## Construction

1. The SNAP edge list was deduplicated: `soc-pokec-relationships.txt.gz` has
   30,622,564 edges and **0 duplicate directed pairs**.
2. User ids were shifted from 1-based to 0-based (`1 .. 1632803` became
   `0 .. 1632802`).
3. The single label is `friend`; the graph is stored as one Boolean
   MatrixMarket file, `graph/friend.mtx`.

The conversion scripts are in `pokec/scripts/soc_pokec/`:
`deduplicate.py` and `convert.py`.

## Nodes

1,632,803 nodes. Each node is an anonymized Pokec user.

## Edges and labels

30,622,564 stored directed edges, all with the label `friend`.

| Edge Label | Num Edge Label |
|---|---:|
| `friend` | 30,622,564 |

Reversed edges (`friend_r`) are derived by FLPQ_Data and are not stored.

## Query classes

RPQ applies: the graph is unlabelled apart from the single `friend` relation,
so regular path queries are natural. This archive ships one RPQ query,
`rpq/2` (language `friend`, exactly one friendship step); its reference
result is the adjacency matrix itself.

CFPQ and MCFPQ are applicable in principle, but this archive contains no
CFPQ/MCFPQ queries with reference results, so those class directories are
empty.

## Files

```
soc-pokec/
├── README.md
├── graph/
│   └── friend.mtx
└── queries/
    ├── README.md
    ├── cfpq/            (empty)
    ├── rpq/2/
    │   ├── query.re
    │   ├── query.rsm
    │   └── results.mtx
    └── mcfpq/           (empty)
```

## License

The source Pokec data is distributed by SNAP; see the source page for its
terms of use. The derived archive is contributed under the FLPQ_Data dataset
license, Creative Commons Attribution 4.0 International (CC-BY 4.0).

## Caveats

- The graph is the full 1.6M-node Pokec graph; no sampling or subgraph was
  applied.
- Only query `rpq/2` is included. The other SPARQL-derived queries from the
  same endpoint set produce all-pairs answers of hundreds of millions to
  trillions of pairs and cannot be stored as a reference `results.mtx`.
- `rpq/2` has exactly one edge per pair, so its reference result has
  30,622,564 pairs and is identical to the stored `friend` adjacency.
- The original SPARQL queries have a fixed source node and a `LIMIT`; the
  RPQ format stores only the language, so those parameters are not part of
  this archive.
