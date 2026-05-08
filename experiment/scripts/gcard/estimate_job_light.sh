#!/bin/bash
set -eu
set -o pipefail

k=${1:-3}
sample_size=${2:-500}
predicate_apply_type=${3:-0}

workspace=$(realpath "$(dirname "$0")/../../")
minigu=${MINIGU_BIN:-$workspace/../target/release/minigu}
db_path=${JOB_LIGHT_GCARD_DB_PATH:-$workspace/datasets/job_light/minigu_db}
pattern_dir=${JOB_LIGHT_GCARD_PATTERN_DIR:-$workspace/pattern_gcard/job_light}
graph_name=${JOB_LIGHT_GCARD_GRAPH:-job_light}
output_dir=$workspace/results/gcard/estimate
output_log=$output_dir/job_light_k${k}_p${predicate_apply_type}_d0.log

mkdir -p "$output_dir"

if [[ ! -x "$minigu" ]]; then
  echo "minigu binary not found: $minigu" >&2
  exit 1
fi
if [[ ! -d "$db_path" ]]; then
  echo "database path not found: $db_path" >&2
  echo "Set JOB_LIGHT_GCARD_DB_PATH to the minigu_db built for JOB-light." >&2
  exit 1
fi
if [[ ! -d "$pattern_dir" ]]; then
  echo "pattern dir not found: $pattern_dir" >&2
  exit 1
fi

echo "# pattern estimate latency_s" > "$output_log"

mapfile -t patterns < <(find "$pattern_dir" -name '*.json' -type f | sort -V)

tmp_script=$(mktemp)
tmp_out=$(mktemp)
tmp_rels=$(mktemp)
trap 'rm -f "$tmp_script" "$tmp_out" "$tmp_rels"' EXIT

{
  echo "session set graph $graph_name"
  echo "call load_catalog(\"$graph_name\")"
} > "$tmp_script"

for pattern in "${patterns[@]}"; do
  rel="${pattern#$pattern_dir/}"
  echo "$rel" >> "$tmp_rels"
  echo ":time call gcard_query(\"$pattern\", $k, $sample_size, $predicate_apply_type, false)" >> "$tmp_script"
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

if len(cards) < len(rels):
    print("\n--- minigu output ---", file=sys.stderr)
    print(text[-4000:], file=sys.stderr)
    sys.exit(1)
PY

echo "Log: $output_log"
