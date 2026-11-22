# Queries

## rpq/2

Language: `friend` — exactly one directed friendship edge.

Every path of length one in the graph satisfies the query, so the reference
`results.mtx` is the `friend` adjacency matrix: 30,622,564 pairs.

The language is represented twice, by `query.re` and `query.rsm`; both files
define the same regular language.

The fixed source node and `LIMIT` from the original SPARQL query are not part
of the RPQ format; this result covers all pairs `(u, v)`.
