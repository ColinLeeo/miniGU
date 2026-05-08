#!/bin/bash
set -eu
set -o pipefail

sf=$1

workspace=$(realpath $(dirname $0)/../../)
source "$workspace/scripts/factorjoin/env.sh"
runner=$workspace/scripts/factorjoin/run_factorjoin.py
patterns=$workspace/pattern_sql/lsqb
model=$workspace/catalogs/ldbc/factorjoin/ldbc_sf$sf.pkl
output_dir=$workspace/results/factorjoin/estimate
output_log=$output_dir/ldbc_sf$sf.log

mkdir -p "$output_dir"

run_factorjoin_python "$runner" estimate \
    --workspace "$workspace" \
    --pattern-dir "$patterns" \
    --model-path "$model" \
    --output-log "$output_log"
