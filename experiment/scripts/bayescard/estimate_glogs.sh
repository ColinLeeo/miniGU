#!/bin/bash
set -eu
set -o pipefail

workspace=$(realpath "$(dirname "$0")/../../")
repro_dir=$workspace/baseline/SafeBound/repro_bayescard_ldbc_sf1
python_bin=${BAYESCARD_PYTHON:-$repro_dir/env/bin/python}
query_dir=${GLOGS_BAYESCARD_QUERY_DIR:-$workspace/pattern_sql/glogs}
model_dir=${GLOGS_BAYESCARD_MODEL_DIR:-$repro_dir/models_full_paper}
output=${GLOGS_BAYESCARD_OUTPUT:-$workspace/results/bayescard/glogs_sf1.csv}

if [[ ! -x "$python_bin" ]]; then
  echo "BayesCard python not found: $python_bin" >&2
  exit 1
fi
if [[ ! -d "$query_dir" ]]; then
  echo "query dir not found: $query_dir" >&2
  exit 1
fi
if [[ ! -d "$model_dir" ]]; then
  echo "model dir not found: $model_dir" >&2
  exit 1
fi

mkdir -p "$(dirname "$output")"

"$python_bin" "$workspace/scripts/bayescard/estimate_glogs.py" \
  --query-dir "$query_dir" \
  --model-dir "$model_dir" \
  --output "$output"
