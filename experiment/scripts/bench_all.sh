#!/usr/bin/env bash
#
# Unified benchmark: run all cardinality estimators on miniGU LDBC patterns.
#
# Usage:
#   ./bench_all.sh <sf> [pattern_dir]
#
# Examples:
#   ./bench_all.sh 1                                       # all LDBC patterns
#   ./bench_all.sh 0.1 experiment/pattern/LDBC/L4_cycle    # specific subset
#
# The script automatically converts miniGU patterns to pathce/G-CARE formats
# at runtime (no pre-generated files needed).
#
# Prerequisites (run once per SF):
#   1. miniGU:  import graph + create_catalog (or load_catalog)
#   2. pathce:  ./prepare_baselines.sh <sf> pathce
#   3. gcare:   ./prepare_baselines.sh <sf> gcare
#   4. color:   ./prepare_baselines.sh <sf> color
#   5. glogs:   ./prepare_baselines.sh <sf> glogs

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
EXPERIMENT_DIR="$PROJECT_DIR/experiment"

SF=${1:?Usage: bench_all.sh <sf> [pattern_dir]}
PATTERN_SOURCE="${2:-$EXPERIMENT_DIR/pattern/LDBC}"

# ── Config ─────────────────────────────────────────────────────────────────
SCHEMA="$EXPERIMENT_DIR/schemas/ldbc/ldbc_pathce_schema.json"
TIMEOUT_SEC=300
GRAPH_NAME="ldbc"
MAX_K=2
SAMPLE_SIZE=500

# Tool binaries
MINIGU="$PROJECT_DIR/target/release/minigu"
PATHCE="$EXPERIMENT_DIR/baseline/pathce/target/release/pathce"
GCARE_GRAPH="$EXPERIMENT_DIR/baseline/gcare/build/gcare_graph"
GLOGS_BIN="$EXPERIMENT_DIR/baseline/glogs/ir/target/release/pattern_count"

# Data paths
MINIGU_DB="$EXPERIMENT_DIR/dataset/ldbc/sf${SF}/minigu_db"
PATHCE_CATALOG="$EXPERIMENT_DIR/catalogs/ldbc/pathce/ldbc_sf${SF}_2_5_200"
PATHCE_GRAPH="$EXPERIMENT_DIR/graphs/ldbc/pathce/ldbc_sf${SF}.bincode"
GCARE_DATA_DIR="$EXPERIMENT_DIR/dataset/ldbc/sf${SF}"
COLOR_SUMMARY="$EXPERIMENT_DIR/catalogs/ldbc/color/ldbc_sf${SF}_mix_6_50000.obj"
GLOGS_CATALOG="$EXPERIMENT_DIR/catalogs/ldbc/glogs/ldbc_sf${SF}.bincode"

RESULT_DIR="$EXPERIMENT_DIR/result/comparison"
mkdir -p "$RESULT_DIR"
OUTPUT_CSV="$RESULT_DIR/comparison_sf${SF}.csv"

log() { echo "[$(date '+%H:%M:%S')] $*"; }

# ── Step 0: Convert patterns ──────────────────────────────────────────────
# Generate temporary pathce/gcare format patterns from miniGU source patterns
TMP_DIR=$(mktemp -d /tmp/bench_all_XXXX)
trap 'rm -rf "$TMP_DIR"' EXIT

PATHCE_PATTERN_DIR="$TMP_DIR/pathce"
GCARE_PATTERN_DIR="$TMP_DIR/gcare"

log "Converting patterns: $PATTERN_SOURCE"
python3 "$EXPERIMENT_DIR/tools/minigu2pathce.py" \
    -s "$SCHEMA" \
    -d "$PATTERN_SOURCE" \
    -o "$PATHCE_PATTERN_DIR" \
    --gcare "$GCARE_PATTERN_DIR"

# Collect miniGU source patterns (the originals).
# Skip patterns with predicates (*_P*.json) — baselines don't support predicates,
# so they'd produce identical results to the base pattern.
MINIGU_PATTERNS=()
while IFS= read -r f; do
    base=$(basename "$f" .json)
    if [[ "$base" == *_P* ]]; then
        log "  skip predicate pattern: $f"
        continue
    fi
    MINIGU_PATTERNS+=("$f")
done < <(find "$PATTERN_SOURCE" -name '*.json' -type f | sort)

log "SF=$SF, patterns=${#MINIGU_PATTERNS[@]}"

# Helper: given a miniGU pattern path, derive the relative name (e.g. L4_cycle/L4)
rel_name() {
    local p="$1"
    local rel="${p#$PATTERN_SOURCE/}"
    echo "${rel%.json}"
}

# ── CSV header ─────────────────────────────────────────────────────────────
echo "sf,pattern_group,pattern,tool,cardinality,time_s,status" > "$OUTPUT_CSV"

append() {
    # $1=group $2=name $3=tool $4=cardinality $5=time_s $6=status
    echo "$SF,$1,$2,$3,$4,$5,$6" >> "$OUTPUT_CSV"
    log "  $3: $1/$2 -> card=$4 time=$5 ($6)"
}

# # ── 1. miniGU (IGNORE predicate for fair comparison) ──────────────────────
# if [[ -x "$MINIGU" ]] && [[ -d "$MINIGU_DB" ]]; then
#     log "=== miniGU ==="
#     TMP_GQL="$TMP_DIR/bench.gql"
#     cat > "$TMP_GQL" <<EOF
# session set graph ${GRAPH_NAME}
# call load_catalog("${GRAPH_NAME}")
# EOF
#     for p in "${MINIGU_PATTERNS[@]}"; do
#         # pred_type=2 means IGNORE (no predicates, fair comparison)
#         echo ":time call gcard_query(\"${p}\", ${MAX_K}, ${SAMPLE_SIZE}, 2, false)" >> "$TMP_GQL"
#     done

#     MINIGU_OUT="$TMP_DIR/minigu_out.txt"
#     timeout ${TIMEOUT_SEC} "$MINIGU" execute "$TMP_GQL" --path "$MINIGU_DB" 2>&1 | tee "$MINIGU_OUT" || true

#     # Parse: each query prints "<name>, cardinality: <N>" then ":time" prints "Time: <X>s"
#     mapfile -t card_lines < <(grep 'cardinality:' "$MINIGU_OUT" | grep -o '[^,]*, cardinality: [0-9.]*')
#     mapfile -t time_lines < <(grep -o 'Time: [0-9.]*s' "$MINIGU_OUT" | grep -o '[0-9.]*')

#     idx=0
#     for p in "${MINIGU_PATTERNS[@]}"; do
#         rn=$(rel_name "$p")
#         group=$(dirname "$rn")
#         name=$(basename "$rn")

#         card="0"; time_s="0"
#         if [[ $idx -lt ${#card_lines[@]} ]]; then
#             card=$(echo "${card_lines[$idx]}" | grep -o 'cardinality: [0-9.]*' | awk '{print $2}') || card="0"
#         fi
#         if [[ $idx -lt ${#time_lines[@]} ]]; then
#             time_s="${time_lines[$idx]}"
#         fi

#         if [[ "$card" == "0" ]]; then
#             append "$group" "$name" "minigu" "0" "$time_s" "error"
#         else
#             append "$group" "$name" "minigu" "$card" "$time_s" "ok"
#         fi
#         idx=$((idx + 1))
#     done
# else
#     log "SKIP miniGU (binary=$MINIGU or db=$MINIGU_DB not found)"
# fi

# ── 2. pathce ─────────────────────────────────────────────────────────────
if [[ -x "$PATHCE" ]] && [[ -d "$PATHCE_CATALOG" ]]; then
    log "=== pathce ==="
    for p in "${MINIGU_PATTERNS[@]}"; do
        rn=$(rel_name "$p")
        group=$(dirname "$rn")
        name=$(basename "$rn")
        pp="$PATHCE_PATTERN_DIR/${rn}.json"

        result=$(timeout ${TIMEOUT_SEC} "$PATHCE" estimate \
            -c "$PATHCE_CATALOG" -p "$pp" \
            --max-path-length 2 --max-star-degree 5 2>&1 || echo "0,0")
        IFS=',' read -r card time_s <<< "$result"
        status="ok"
        [[ "$card" == "0" ]] && [[ "$time_s" == "0" ]] && status="error"
        append "$group" "$name" "pathce" "$card" "$time_s" "$status"
    done
else
    log "SKIP pathce (binary or catalog not found)"
fi

# ── 3. gcare (WanderJoin) ────────────────────────────────────────────────
if [[ -x "$GCARE_GRAPH" ]] && [[ -d "$GCARE_DATA_DIR" ]]; then
    log "=== gcare-wj ==="
    for p in "${MINIGU_PATTERNS[@]}"; do
        rn=$(rel_name "$p")
        group=$(dirname "$rn")
        name=$(basename "$rn")
        gp="$GCARE_PATTERN_DIR/${rn}.txt"

        # gcare outputs "est,time\n" on stdout (comma-separated)
        result=$(timeout ${TIMEOUT_SEC} \
            "$GCARE_GRAPH" -q -m wj -n 1 -i "$gp" -d "$GCARE_DATA_DIR" 2>/dev/null \
            | tail -1 || echo "")
        if [[ "$result" == *","* ]]; then
            IFS=',' read -r card time_s <<< "$result"
            append "$group" "$name" "gcare-wj" "$card" "$time_s" "ok"
        else
            append "$group" "$name" "gcare-wj" "0" "0" "error"
        fi
    done
else
    log "SKIP gcare (binary or data not found)"
fi

# ── 4. color ──────────────────────────────────────────────────────────────
if command -v julia &>/dev/null && [[ -f "$COLOR_SUMMARY" ]]; then
    log "=== color ==="
    COLOR_DIR="$EXPERIMENT_DIR/baseline/color"
    for p in "${MINIGU_PATTERNS[@]}"; do
        rn=$(rel_name "$p")
        group=$(dirname "$rn")
        name=$(basename "$rn")
        gp="$GCARE_PATTERN_DIR/${rn}.txt"

        result=$(timeout ${TIMEOUT_SEC} \
            julia --project="$COLOR_DIR" "$COLOR_DIR/scripts/estimate.jl" \
            -q "$gp" -s "$COLOR_SUMMARY" 2>/dev/null || echo "0,0")
        IFS=',' read -r card time_s <<< "$result"
        append "$group" "$name" "color" "$card" "$time_s" "ok"
    done
else
    log "SKIP color (julia or summary not found)"
fi

# ── 5. glogs ──────────────────────────────────────────────────────────────
if [[ -x "$GLOGS_BIN" ]] && [[ -f "$GLOGS_CATALOG" ]]; then
    log "=== glogs ==="
    for p in "${MINIGU_PATTERNS[@]}"; do
        rn=$(rel_name "$p")
        group=$(dirname "$rn")
        name=$(basename "$rn")
        pp="$PATHCE_PATTERN_DIR/${rn}.json"

        result=$(timeout ${TIMEOUT_SEC} "$GLOGS_BIN" -c "$GLOGS_CATALOG" -p "$pp" 2>/dev/null || echo "0,0")
        IFS=',' read -r card time_s <<< "$result"
        append "$group" "$name" "glogs" "$card" "$time_s" "ok"
    done
else
    log "SKIP glogs (binary or catalog not found)"
fi

# ── 6. Ground truth (pathce count) ───────────────────────────────────────
if [[ -x "$PATHCE" ]] && [[ -f "$PATHCE_GRAPH" ]]; then
    log "=== ground truth (pathce count) ==="
    for p in "${MINIGU_PATTERNS[@]}"; do
        rn=$(rel_name "$p")
        group=$(dirname "$rn")
        name=$(basename "$rn")
        pp="$PATHCE_PATTERN_DIR/${rn}.json"

        result=$(timeout ${TIMEOUT_SEC} "$PATHCE" count -g "$PATHCE_GRAPH" -p "$pp" -s path 2>/dev/null || echo "timeout")
        if [[ "$result" == "timeout" ]]; then
            append "$group" "$name" "truth" "-1" "0" "timeout"
        else
            append "$group" "$name" "truth" "$result" "0" "ok"
        fi
    done
else
    log "SKIP ground truth (pathce binary or graph not found)"
fi

log "Done. Results: $OUTPUT_CSV"
echo ""
echo "=== Results ==="
column -t -s',' "$OUTPUT_CSV"
