#!/bin/bash
set -eu
set -o pipefail

sf=$1
k=$2
sample_size=${3:-500}

workspace=$(realpath "$(dirname "$0")/../../")
minigu=$workspace/../target/release/minigu
db_path=$workspace/datasets/ldbc/sf$sf/minigu_db
patterns_dir=$workspace/pattern_gcard/lsqb
graph_name=ldbc

output_dir=$workspace/results/gcard/estimate
output_log=$output_dir/lsqb_sf${sf}_k${k}_d0.log
mkdir -p "$output_dir"

if [[ ! -x "$minigu" ]]; then
  echo "minigu binary not found: $minigu" >&2
  exit 1
fi
if [[ ! -d "$db_path" ]]; then
  echo "database path not found: $db_path" >&2
  exit 1
fi
if [[ ! -d "$patterns_dir" ]]; then
  echo "pattern directory not found: $patterns_dir" >&2
  exit 1
fi

echo "# pattern estimate latency_s" > "$output_log"

mapfile -t patterns < <(python3 - "$patterns_dir" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
for p in sorted(root.rglob("*.json")):
    print(str(p.resolve()))
PY
)

tmp_script=$(mktemp)
tmp_out=$(mktemp)
tmp_rels=$(mktemp)

{
  echo "session set graph $graph_name"
  echo "call load_catalog(\"$graph_name\")"
} > "$tmp_script"

for pattern in "${patterns[@]}"; do
  rel="${pattern#$patterns_dir/}"
  echo "$rel" >> "$tmp_rels"
  echo ":time call gcard_query(\"$pattern\", $k, $sample_size, 0, false)" >> "$tmp_script"
done

"$minigu" execute "$tmp_script" --path "$db_path" >"$tmp_out" 2>&1

python3 - "$tmp_out" "$tmp_rels" "$output_log" <<'PY'
import re
import sys

out_path, rels_path, log_path = sys.argv[1], sys.argv[2], sys.argv[3]
text = open(out_path, "r", encoding="utf-8", errors="ignore").read()
rels = [l.strip() for l in open(rels_path, "r", encoding="utf-8") if l.strip()]

cards = re.findall(r"cardinality:\s*([0-9]+)", text)
times = re.findall(r"Time:\s*([0-9.]+)s", text)

with open(log_path, "a", encoding="utf-8") as f:
    for i, rel in enumerate(rels):
        card = cards[i] if i < len(cards) else "0"
        lat = times[i] if i < len(times) else "0"
        f.write(f"{rel} {card} {lat}\n")
        print(f"{rel} -> estimate={card} latency={lat}s")
PY

rm -f "$tmp_script" "$tmp_out" "$tmp_rels"

echo "Log: $output_log"
