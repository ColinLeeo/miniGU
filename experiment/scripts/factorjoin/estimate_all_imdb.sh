#!/bin/bash
set -eu
set -o pipefail

workspace=$(realpath $(dirname $0)/../../)
source "$workspace/scripts/factorjoin/env.sh"
runner=$workspace/scripts/factorjoin/run_factorjoin.py
patterns=$workspace/pattern_sql/imdb
model=$workspace/catalogs/imdb/factorjoin/imdb.pkl
output_dir=$workspace/results/factorjoin/estimate
output_log=$output_dir/imdb.log

mkdir -p "$output_dir"

run_factorjoin_python "$runner" estimate \
    --workspace "$workspace" \
    --pattern-dir "$patterns" \
    --model-path "$model" \
    --output-log "$output_log"
