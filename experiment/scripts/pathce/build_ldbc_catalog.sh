#!/bin/bash
set -eu
set -o pipefail

sf=$1
threads=$2
k=$3
d=$4
m=$5

workspace=$(realpath $(dirname $0)/../../)
source "$workspace/scripts/common/build_metrics.sh"
output_dir=$workspace/catalogs/ldbc/pathce
mkdir -p $output_dir
output=$output_dir/ldbc_"sf$sf"_"$k"_"$d"_"$m"
log=$workspace/results/pathce/build-logs/ldbc_sf"$sf"_"$k"_"$d"_"$m".log

run_build_with_metrics "$log" "PathCE" "ldbc_sf$sf" "$threads" "$output" \
  "$workspace/baseline/pathce/target/release/pathce" analyze -s "$workspace/schemas/ldbc/ldbc_pathce_schema.json" -g "$workspace/graphs/ldbc/pathce/ldbc_sf$sf.bincode" --greedy -t "$threads" -o "$output" --max-path-length "$k" --max-star-degree "$d" --buckets "$m"
