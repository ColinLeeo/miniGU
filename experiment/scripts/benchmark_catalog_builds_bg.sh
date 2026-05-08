#!/bin/bash
set -Eeuo pipefail

script_path=$(realpath "$0")
workspace=$(realpath "$(dirname "$0")/..")

ldbc_sf=${LDBC_SF:-0.1}
threads=${THREADS:-64}
gcard_k=${GCARD_K:-3}
pathce_k=${PATHCE_K:-3}
pathce_d=${PATHCE_D:-0}
pathce_m=${PATHCE_M:-200}
bayescard_sample_size=${BAYESCARD_SAMPLE_SIZE:-10000}
bayescard_join_sample_size=${BAYESCARD_JOIN_SAMPLE_SIZE:-50000}
cpuset=${CPUSET:-}
run_id=${RUN_ID:-$(date +%Y%m%d_%H%M%S)}
log_dir=${LOG_DIR:-$workspace/results/catalog-build-benchmark/$run_id}

usage() {
  cat <<EOF
Usage: $(basename "$0") [--foreground] [options]

Default behavior starts the benchmark with nohup in the background.

Options:
  --ldbc-sf SF                 LDBC scale factor, default: $ldbc_sf
  --threads N                  Threads for all thread-aware builders, default: $threads
  --gcard-k K                  GCard catalog k, default: $gcard_k
  --pathce-k K                 PathCE max path length, default: $pathce_k
  --pathce-d D                 PathCE max star degree, default: $pathce_d
  --pathce-m M                 PathCE buckets, default: $pathce_m
  --bayescard-sample-size N    BayesCard structure sample size, default: $bayescard_sample_size
  --bayescard-join-sample N    BayesCard join sample size, default: $bayescard_join_sample_size
  --cpuset CPUSET              CPU affinity for each case, default: 0-(threads-1)
  --log-dir DIR                Benchmark output dir, default: $log_dir
  -h, --help                   Show this help.
EOF
}

foreground=0
forward_args=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --foreground) foreground=1; shift ;;
    --ldbc-sf) ldbc_sf=$2; forward_args+=("$1" "$2"); shift 2 ;;
    --threads) threads=$2; forward_args+=("$1" "$2"); shift 2 ;;
    --gcard-k) gcard_k=$2; forward_args+=("$1" "$2"); shift 2 ;;
    --pathce-k) pathce_k=$2; forward_args+=("$1" "$2"); shift 2 ;;
    --pathce-d) pathce_d=$2; forward_args+=("$1" "$2"); shift 2 ;;
    --pathce-m) pathce_m=$2; forward_args+=("$1" "$2"); shift 2 ;;
    --bayescard-sample-size) bayescard_sample_size=$2; forward_args+=("$1" "$2"); shift 2 ;;
    --bayescard-join-sample) bayescard_join_sample_size=$2; forward_args+=("$1" "$2"); shift 2 ;;
    --cpuset) cpuset=$2; forward_args+=("$1" "$2"); shift 2 ;;
    --log-dir) log_dir=$(realpath -m "$2"); forward_args+=("$1" "$log_dir"); shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

mkdir -p "$log_dir"
if [[ -z "$cpuset" ]]; then
  cpuset="0-$((threads - 1))"
fi
if [[ "$foreground" -eq 0 ]]; then
  nohup "$script_path" --foreground "${forward_args[@]}" > "$log_dir/driver.log" 2>&1 &
  echo "Started catalog-build benchmark in background."
  echo "PID: $!"
  echo "Driver log: $log_dir/driver.log"
  echo "Summary CSV: $log_dir/summary.csv"
  exit 0
fi

artifact_dir=$log_dir/artifacts
summary_csv=$log_dir/summary.csv
mkdir -p "$artifact_dir"
trap 'rc=$?; echo "[$(date -Is)] FATAL line=$LINENO exit=$rc" | tee -a "$log_dir/driver.log"; exit "$rc"' ERR
: > "$summary_csv"
echo "case_id,method,dataset,threads,exit_code,start,end,elapsed_s,max_rss_kb,output_bytes,case_log,command" >> "$summary_csv"

export OMP_NUM_THREADS=$threads
export OPENBLAS_NUM_THREADS=$threads
export MKL_NUM_THREADS=$threads
export RAYON_NUM_THREADS=$threads
export JULIA_NUM_THREADS=$threads

minigu=$workspace/../target/release/minigu
pathce_bin=$workspace/baseline/pathce/target/release/pathce
julia_bin=${JULIA_BIN:-julia}
if ! command -v "$julia_bin" >/dev/null 2>&1; then
  julia_bin=$HOME/.local/bin/julia
fi

csv_escape() {
  local s=${1//\"/\"\"}
  printf '"%s"' "$s"
}

path_size_bytes() {
  local p=$1
  if [[ -e "$p" ]]; then
    du -sb "$p" 2>/dev/null | awk '{print $1}'
  else
    echo 0
  fi
}

max_rss_from_time() {
  local f=$1
  if [[ -s "$f" ]]; then
    awk -F: '/Maximum resident set size/ {gsub(/^[ \t]+/, "", $2); print $2}' "$f"
  else
    echo 0
  fi
}

assert_missing() {
  local p=$1
  if [[ -e "$p" ]]; then
    echo "refuse to overwrite existing artifact: $p" >&2
    exit 1
  fi
}

require_path() {
  local p=$1
  if [[ ! -e "$p" ]]; then
    echo "required path not found: $p" >&2
    exit 1
  fi
}

has_paths() {
  local p
  for p in "$@"; do
    if [[ ! -e "$p" ]]; then
      echo "missing prerequisite: $p" >&2
      return 1
    fi
  done
}

record_skip() {
  local case_id=$1
  local method=$2
  local dataset=$3
  local reason=$4
  local case_log=$log_dir/${case_id}.log
  local now
  now=$(date -Is)
  {
    echo "case_id=$case_id"
    echo "method=$method"
    echo "dataset=$dataset"
    echo "threads=$threads"
    echo "start=$now"
    echo "end=$now"
    echo "exit_code=SKIPPED"
    echo "reason=$reason"
  } > "$case_log"
  printf '%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,' "$case_id" "$method" "$dataset" "0" "SKIPPED" "$now" "$now" "0" "0" "0" >> "$summary_csv"
  csv_escape "$case_log" >> "$summary_csv"
  printf ',' >> "$summary_csv"
  csv_escape "$reason" >> "$summary_csv"
  printf '\n' >> "$summary_csv"
  echo "[$now] SKIP $case_id $method $dataset: $reason" | tee -a "$log_dir/driver.log"
}

prepare_gcard_db_copy() {
  local source_db=$1
  local target_db=$2
  local graph_name=$3
  require_path "$source_db"
  assert_missing "$target_db"
  mkdir -p "$(dirname "$target_db")"
  cp -a "$source_db" "$target_db"
  rm -f "$target_db/${graph_name}.statistic.bin"
}

run_case() {
  local case_id=$1
  local method=$2
  local dataset=$3
  local output_path=$4
  shift 4

  assert_missing "$output_path"

  local case_log=$log_dir/${case_id}.log
  local time_file=$log_dir/${case_id}.time
  local command_text start_iso end_iso start_epoch end_epoch elapsed_s status max_rss_kb output_bytes
  command_text=$(printf '%q ' "$@")
  start_iso=$(date -Is)
  start_epoch=$(date +%s)
  status=0

  {
    echo "case_id=$case_id"
    echo "method=$method"
    echo "dataset=$dataset"
    echo "threads=$threads"
    echo "output_path=$output_path"
    echo "start=$start_iso"
    echo "command=$command_text"
    echo
  } > "$case_log"

  echo "[$start_iso] START $case_id $method $dataset" | tee -a "$log_dir/driver.log"
  if command -v /usr/bin/time >/dev/null 2>&1; then
    if command -v taskset >/dev/null 2>&1; then
      /usr/bin/time -v -o "$time_file" taskset -c "$cpuset" "$@" >> "$case_log" 2>&1 || status=$?
    else
      /usr/bin/time -v -o "$time_file" "$@" >> "$case_log" 2>&1 || status=$?
    fi
  else
    if command -v taskset >/dev/null 2>&1; then
      taskset -c "$cpuset" "$@" >> "$case_log" 2>&1 || status=$?
    else
      "$@" >> "$case_log" 2>&1 || status=$?
    fi
  fi

  end_iso=$(date -Is)
  end_epoch=$(date +%s)
  elapsed_s=$((end_epoch - start_epoch))
  max_rss_kb=$(max_rss_from_time "$time_file")
  max_rss_kb=${max_rss_kb:-0}
  output_bytes=$(path_size_bytes "$output_path")

  {
    echo
    echo "end=$end_iso"
    echo "exit_code=$status"
    echo "elapsed_s=$elapsed_s"
    echo "max_rss_kb=$max_rss_kb"
    echo "output_bytes=$output_bytes"
    if [[ -s "$time_file" ]]; then
      echo
      echo "[/usr/bin/time -v]"
      cat "$time_file"
    fi
  } >> "$case_log"

  printf '%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,' "$case_id" "$method" "$dataset" "$threads" "$status" "$start_iso" "$end_iso" "$elapsed_s" "$max_rss_kb" "$output_bytes" >> "$summary_csv"
  csv_escape "$case_log" >> "$summary_csv"
  printf ',' >> "$summary_csv"
  csv_escape "$command_text" >> "$summary_csv"
  printf '\n' >> "$summary_csv"

  echo "[$end_iso] END $case_id exit=$status elapsed=${elapsed_s}s max_rss=${max_rss_kb}KB output=${output_bytes}B" | tee -a "$log_dir/driver.log"
}

make_gcard_query() {
  local graph_name=$1
  local k=$2
  local query_file=$3
  cat > "$query_file" <<EOF
session set graph $graph_name
call create_catalog("$graph_name", $k, $threads, $threads)
EOF
}

echo "run_id=$run_id" | tee -a "$log_dir/driver.log"
echo "workspace=$workspace" | tee -a "$log_dir/driver.log"
echo "ldbc_sf=$ldbc_sf threads=$threads cpuset=$cpuset" | tee -a "$log_dir/driver.log"
if command -v nproc >/dev/null 2>&1; then
  echo "nproc=$(nproc)" | tee -a "$log_dir/driver.log"
fi

gcard_ldbc_db=$artifact_dir/gcard/ldbc_sf${ldbc_sf}/minigu_db
gcard_imdb_db=$artifact_dir/gcard/imdb/minigu_db
gcard_ldbc_query=$log_dir/gcard_ldbc.query
gcard_imdb_query=$log_dir/gcard_imdb.query

if has_paths "$minigu" "$workspace/datasets/ldbc/sf$ldbc_sf/minigu_db"; then
  prepare_gcard_db_copy "$workspace/datasets/ldbc/sf$ldbc_sf/minigu_db" "$gcard_ldbc_db" ldbc
  make_gcard_query ldbc "$gcard_k" "$gcard_ldbc_query"
  run_case "01_gcard_ldbc" "gcard" "ldbc_sf$ldbc_sf" "$gcard_ldbc_db/ldbc.statistic.bin" \
    "$minigu" execute "$gcard_ldbc_query" --path "$gcard_ldbc_db"
else
  record_skip "01_gcard_ldbc" "gcard" "ldbc_sf$ldbc_sf" "missing minigu or LDBC minigu_db"
fi

if has_paths "$minigu" "$workspace/datasets/imdb/imdb/minigu_db"; then
  prepare_gcard_db_copy "$workspace/datasets/imdb/imdb/minigu_db" "$gcard_imdb_db" imdb
  make_gcard_query imdb "$gcard_k" "$gcard_imdb_query"
  run_case "02_gcard_imdb" "gcard" "imdb" "$gcard_imdb_db/imdb.statistic.bin" \
    "$minigu" execute "$gcard_imdb_query" --path "$gcard_imdb_db"
else
  record_skip "02_gcard_imdb" "gcard" "imdb" "missing minigu or IMDB minigu_db"
fi

pathce_ldbc_out=$artifact_dir/pathce/ldbc_sf${ldbc_sf}_${pathce_k}_${pathce_d}_${pathce_m}
pathce_imdb_out=$artifact_dir/pathce/imdb_${pathce_k}_${pathce_d}_${pathce_m}
mkdir -p "$artifact_dir/pathce"
if has_paths "$pathce_bin" "$workspace/schemas/ldbc/ldbc_pathce_schema.json" "$workspace/graphs/ldbc/pathce/ldbc_sf$ldbc_sf.bincode"; then
  run_case "03_pathce_ldbc" "pathce" "ldbc_sf$ldbc_sf" "$pathce_ldbc_out" \
    "$pathce_bin" analyze -s "$workspace/schemas/ldbc/ldbc_pathce_schema.json" -g "$workspace/graphs/ldbc/pathce/ldbc_sf$ldbc_sf.bincode" --greedy -t "$threads" -o "$pathce_ldbc_out" --max-path-length "$pathce_k" --max-star-degree "$pathce_d" --buckets "$pathce_m"
else
  record_skip "03_pathce_ldbc" "pathce" "ldbc_sf$ldbc_sf" "missing pathce binary/schema/graph"
fi

if has_paths "$pathce_bin" "$workspace/schemas/imdb/imdb_pathce_schema.json" "$workspace/graphs/imdb/pathce/imdb.bincode"; then
  run_case "04_pathce_imdb" "pathce" "imdb" "$pathce_imdb_out" \
    "$pathce_bin" analyze -s "$workspace/schemas/imdb/imdb_pathce_schema.json" -g "$workspace/graphs/imdb/pathce/imdb.bincode" --greedy -t "$threads" -o "$pathce_imdb_out" --max-path-length "$pathce_k" --max-star-degree "$pathce_d" --buckets "$pathce_m"
else
  record_skip "04_pathce_imdb" "pathce" "imdb" "missing pathce binary/schema/graph"
fi

color_ldbc_out=$artifact_dir/color/ldbc_sf${ldbc_sf}_mix_6_50000.obj
color_imdb_out=$artifact_dir/color/imdb_mix_6_50000.obj
mkdir -p "$artifact_dir/color"
if has_paths "$julia_bin" "$workspace/baseline/color/scripts/build.jl" "$workspace/datasets/ldbc/sf$ldbc_sf/ldbc_sf${ldbc_sf}_gcare.txt"; then
  run_case "05_color_ldbc" "color" "ldbc_sf$ldbc_sf" "$color_ldbc_out" \
    "$julia_bin" --project="$workspace/baseline/color" "$workspace/baseline/color/scripts/build.jl" -d "$workspace/datasets/ldbc/sf$ldbc_sf/ldbc_sf${ldbc_sf}_gcare.txt" -o "$color_ldbc_out"
else
  record_skip "05_color_ldbc" "color" "ldbc_sf$ldbc_sf" "missing julia/color script/LDBC gcare graph"
fi

if has_paths "$julia_bin" "$workspace/baseline/color/scripts/build.jl" "$workspace/datasets/imdb/imdb/imdb_gcare.txt"; then
  run_case "06_color_imdb" "color" "imdb" "$color_imdb_out" \
    "$julia_bin" --project="$workspace/baseline/color" "$workspace/baseline/color/scripts/build.jl" -d "$workspace/datasets/imdb/imdb/imdb_gcare.txt" -o "$color_imdb_out"
else
  record_skip "06_color_imdb" "color" "imdb" "missing julia/color script/IMDB gcare graph"
fi

bayes_repro=$workspace/baseline/SafeBound/repro_bayescard_ldbc_sf1
bayes_python=${BAYESCARD_PYTHON:-$bayes_repro/env/bin/python}
bayes_ldbc_hdf=$artifact_dir/bayescard/hdf_ldbc_sf${ldbc_sf}
bayes_ldbc_models=$artifact_dir/bayescard/models_ldbc_sf${ldbc_sf}_sample${bayescard_sample_size}_join${bayescard_join_sample_size}
bayes_imdb_hdf=$artifact_dir/bayescard/hdf_imdb
bayes_imdb_models=$artifact_dir/bayescard/models_imdb_sample${bayescard_sample_size}
mkdir -p "$artifact_dir/bayescard"

if has_paths "$bayes_python" "$bayes_repro/scripts/01_generate_hdf_full.py" "$bayes_repro/scripts/02_train_bn_full.py" "$workspace/datasets/ldbc/sf$ldbc_sf"; then
  assert_missing "$bayes_ldbc_hdf"
  run_case "07_bayescard_ldbc" "bayescard" "ldbc_sf$ldbc_sf" "$bayes_ldbc_models" \
    bash -lc 'set -eu; "$1" "$2/scripts/01_generate_hdf_full.py" --data-dir "$3" --hdf-dir "$4"; "$1" "$2/scripts/02_train_bn_full.py" --data-dir "$3" --hdf-dir "$4" --model-dir "$5" --sample-size "$6" --join-sample-size "$7" --start-index 0 --end-index 50 --overwrite' \
    _ "$bayes_python" "$bayes_repro" "$workspace/datasets/ldbc/sf$ldbc_sf" "$bayes_ldbc_hdf" "$bayes_ldbc_models" "$bayescard_sample_size" "$bayescard_join_sample_size"
else
  record_skip "07_bayescard_ldbc" "bayescard" "ldbc_sf$ldbc_sf" "missing bayescard python/scripts or LDBC data"
fi

bayes_imdb_dir=$workspace/baseline/BayesCard
if has_paths "$bayes_python" "$bayes_imdb_dir/run_experiment.py" "$workspace/datasets/imdb/imdb"; then
  assert_missing "$bayes_imdb_hdf"
  run_case "08_bayescard_imdb" "bayescard" "imdb" "$bayes_imdb_models" \
    bash -lc 'set -eu; cd "$1"; "$2" run_experiment.py --dataset imdb --generate_hdf --csv_path "$3" --hdf_path "$4"; "$2" run_experiment.py --dataset imdb --generate_models --csv_path "$3" --hdf_path "$4" --model_path "$5" --sample_size "$6"' \
    _ "$bayes_imdb_dir" "$bayes_python" "$workspace/datasets/imdb/imdb" "$bayes_imdb_hdf" "$bayes_imdb_models" "$bayescard_sample_size"
else
  record_skip "08_bayescard_imdb" "bayescard" "imdb" "missing bayescard python/run_experiment.py or IMDB data"
fi

echo "summary=$summary_csv" | tee -a "$log_dir/driver.log"
