#!/bin/bash
set -eu
set -o pipefail

threads=${1:-8}

workspace=$(realpath $(dirname $0)/../../)
source "$workspace/scripts/common/build_metrics.sh"
source "$workspace/scripts/factorjoin/env.sh"
python_bin=$(resolve_factorjoin_python)
runner=$workspace/scripts/factorjoin/run_factorjoin.py
dataset=$workspace/datasets/imdb/imdb
patterns=$workspace/pattern_sql/imdb
output_dir=$workspace/catalogs/imdb/factorjoin
output_model=$output_dir/imdb.pkl
log=$workspace/results/factorjoin/build-logs/imdb.log

mkdir -p "$output_dir"

run_build_with_metrics "$log" "FactorJoin" "imdb" "$threads" "$output_model" \
    env -u VIRTUAL_ENV -u PYTHONHOME -u PYTHONPATH -u PYTHONUSERBASE \
    -u CONDA_PREFIX -u CONDA_DEFAULT_ENV -u CONDA_SHLVL -u CONDA_PYTHON_EXE \
    PATH="$(dirname "$python_bin"):/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
    "$python_bin" "$runner" build \
    --workspace "$workspace" \
    --dataset-dir "$dataset" \
    --pattern-dir "$patterns" \
    --output-model "$output_model" \
    --n-dim-dist 1 \
    --n-bins 200 \
    --bucket-method fixed_start_key
