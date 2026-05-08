#!/bin/bash
set -eu
set -o pipefail

_reserved=${1:-1}
threads=$2
k=$3
scan_threads=${4:-$threads}

workspace=$(realpath "$(dirname "$0")/../../")
source "$workspace/scripts/common/build_metrics.sh"
dataset=$workspace/datasets/imdb/imdb
db_path=$dataset/minigu_db
minigu=$workspace/../target/release/minigu
graph_name=imdb
script_dir=$(realpath "$(dirname "$0")")
log=$workspace/results/gcard/build-logs/imdb_k"$k".log
query_file=$(mktemp)
trap 'rm -f "$query_file"' EXIT

# Auto import graph if missing in this database.
catalog_json=$db_path/catalog.json
if [[ ! -f "$catalog_json" ]] || ! python3 -c "import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if sys.argv[2] in d.get('graphs',{}) else 1)" "$catalog_json" "$graph_name"; then
  "$script_dir/build_imdb_graph.sh"
fi

cat > "$query_file" <<EOF
session set graph $graph_name
call create_catalog("$graph_name", $k, $threads, $scan_threads)
EOF

run_build_with_metrics "$log" "GCard" "imdb" "$threads" "$db_path" \
  "$minigu" execute "$query_file" --path "$db_path"
