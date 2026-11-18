# data-init snapshot

`taipei-graph.dump` + `taipei-graph.meta` are produced by
`bash infra/data-init/export-snapshot.sh` from a stack where `data-init`
already finished. When both are present and the fingerprint in `.meta`
matches the current graph SQL / style / bbox, `data-init` restores the dump
in seconds instead of running the full OSM pipeline.

Force a full rebuild anyway with `FORCE_REBUILD=1`.
