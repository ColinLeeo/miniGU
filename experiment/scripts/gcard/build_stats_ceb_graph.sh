#!/bin/bash
set -eu
set -o pipefail

workspace=$(realpath "$(dirname "$0")/../../")
dataset=$workspace/datasets/stats_ceb
manifest=$dataset/manifest.json
db_path=$dataset/minigu_db
minigu=$workspace/../target/release/minigu
graph_name=stats_ceb

if [[ ! -x "$minigu" ]]; then
  echo "minigu binary not found: $minigu" >&2
  exit 1
fi
if [[ ! -f "$manifest" ]]; then
  echo "manifest not found: $manifest" >&2
  exit 1
fi

mkdir -p "$db_path"
manifest_abs=$(realpath "$manifest")

"$minigu" execute /dev/stdin --path "$db_path" <<EOF
call import_graph("$graph_name", "$manifest_abs")
EOF
