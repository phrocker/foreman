#!/bin/sh
# Run only before foreman-store.service starts, while no process owns the store.
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$(readlink -f -- "$0")")" && pwd)
engine=${FOREMAN_SHOAL_EMBED:-"$HOME/.shoal/bin/shoal-embed"}
data=${FOREMAN_STORE_DATA:-"$HOME/.shoal/foreman"}
schema=${FOREMAN_GRAPH_SCHEMA:-"$here/graph-schema.json"}

if [ ! -f "$data/graph/table.json" ]; then
  "$engine" init --data "$data" --table graph --splits 'ent:,evt:'
fi

# Put is idempotent for identical descriptor bytes and rejects conflicting
# edits to an existing version. A newer installed schema belongs to the store.
current=$(mktemp)
trap 'rm -f "$current"' EXIT HUP INT TERM
if "$engine" schema get --data "$data" --table graph > "$current" 2>/dev/null; then
  current_version=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$current")
  desired_version=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$schema")
  if [ "$current_version" -gt "$desired_version" ]; then
    exit 0
  fi
fi
"$engine" schema put --data "$data" --table graph --file "$schema"
