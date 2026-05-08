#!/bin/bash
set -eu
set -o pipefail

sf=$1

workspace=$(realpath "$(dirname "$0")/../../")
dataset=$workspace/datasets/ldbc/sf$sf
schema=$workspace/schemas/ldbc/ldbc_pathce_schema.json
manifest=$dataset/manifest.json
graph_name=ldbc
db_path=$dataset/minigu_db
minigu=$workspace/../target/release/minigu

python3 - "$schema" "$manifest" <<'PY'
import json
import os
import sys

schema_path = sys.argv[1]
manifest_path = sys.argv[2]
dataset_dir = os.path.dirname(manifest_path)

with open(schema_path, "r", encoding="utf-8") as f:
    schema = json.load(f)

vertex_labels = schema["vertex_labels"]
edge_labels = schema["edge_labels"]
edges = schema["edges"]

vid_to_name = {vid: name for name, vid in vertex_labels.items()}
eid_to_name = {eid: name for name, eid in edge_labels.items()}

manifest = {"vertices": [], "edges": []}

for vid, name in sorted(vid_to_name.items()):
    file_name = f"{name}.csv"
    file_path = os.path.join(dataset_dir, file_name)
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"missing vertex csv: {file_path}")
    manifest["vertices"].append(
        {
            "label": name.lower(),
            "file": {"path": file_name, "format": "csv"},
            "properties": [],
        }
    )

for e in sorted(edges, key=lambda x: x["label"]):
    edge_name = eid_to_name[e["label"]]
    src_name = vid_to_name[e["from"]]
    dst_name = vid_to_name[e["to"]]
    file_name = f"{edge_name}.csv"
    file_path = os.path.join(dataset_dir, file_name)
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"missing edge csv: {file_path}")
    manifest["edges"].append(
        {
            "label": edge_name.lower(),
            "src_label": src_name.lower(),
            "dst_label": dst_name.lower(),
            "file": {"path": file_name, "format": "csv"},
            "properties": [],
        }
    )

with open(manifest_path, "w", encoding="utf-8") as f:
    json.dump(manifest, f, indent=2)
PY

manifest_abs=$(realpath "$manifest")

"$minigu" execute /dev/stdin --path "$db_path" <<EOF
call import_graph("$graph_name", "$manifest_abs")
EOF
