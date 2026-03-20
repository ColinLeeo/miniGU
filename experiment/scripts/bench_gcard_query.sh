#!/usr/bin/env bash
#
# Benchmark: gcard_query across all LDBC patterns and scale factors.
#
# Usage:
#   ./bench_gcard_query.sh                    # default: sf0.1 sf0.3 sf1
#   ./bench_gcard_query.sh sf0.1 sf1          # specify scale factors
#
# Before running:
#   1. Each SF should have imported graph + built catalog (statistic.bin)
#   2. Build minigu in release mode: cargo build --release --bin=minigu
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

MINIGU="$PROJECT_DIR/target/release/minigu"
PATTERN_DIR="$PROJECT_DIR/experiment/pattern/LDBC"

# Default scale factors if none specified
if [[ $# -eq 0 ]]; then
    SCALE_FACTORS=(sf0.1 sf0.3 sf1)
else
    SCALE_FACTORS=("$@")
fi

GRAPH_NAME="ldbc"
MAX_K=2
SAMPLE_SIZE=500

# predicate_apply_type: 0 = INNER, 1 = OUTER
PRED_TYPES=(0 1)
PRED_NAMES=("INNER" "OUTER")

RESULT_DIR="$PROJECT_DIR/experiment/result/gcard_query"
mkdir -p "$RESULT_DIR"
OUTPUT_CSV="$RESULT_DIR/gcard_query_results.csv"

log() {
    echo "[$(date '+%H:%M:%S')] $*"
}

# Check binary
if [[ ! -x "$MINIGU" ]]; then
    echo "ERROR: minigu binary not found at $MINIGU"
    echo "Run: cargo build --release --bin=minigu"
    exit 1
fi

# Collect all pattern JSON files
PATTERNS=()
while IFS= read -r f; do
    PATTERNS+=("$f")
done < <(find "$PATTERN_DIR" -name '*.json' -type f | sort)

log "Starting gcard_query benchmark"
log "Scale factors: ${SCALE_FACTORS[*]}"
log "Patterns: ${#PATTERNS[@]}"
log "Predicate types: ${PRED_NAMES[*]}"
log "Output: $OUTPUT_CSV"

# CSV header
echo "sf,pattern_group,pattern,pred_type,pred_type_name,cardinality,time_s" > "$OUTPUT_CSV"

TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

for SF in "${SCALE_FACTORS[@]}"; do
    DB_PATH="$PROJECT_DIR/experiment/dataset/ldbc/$SF/minigu_db"

    if [[ ! -d "$DB_PATH" ]]; then
        log "SKIP $SF: database not found at $DB_PATH"
        continue
    fi

    log "=== Scale Factor: $SF ==="

    # Build a single GQL script for this SF: load graph + catalog once, run all queries
    script_file="$TMP_DIR/bench_${SF}.gql"
    cat > "$script_file" <<EOGQL
session set graph ${GRAPH_NAME}
call load_catalog("${GRAPH_NAME}")
EOGQL

    for pattern_path in "${PATTERNS[@]}"; do
        for pred_type in "${PRED_TYPES[@]}"; do
            echo ":time call gcard_query(\"${pattern_path}\", ${MAX_K}, ${SAMPLE_SIZE}, ${pred_type}, false)" >> "$script_file"
        done
    done

    # Run single minigu process for all queries in this SF
    stdout_file="$TMP_DIR/out_${SF}.txt"
    log "  Running ${#PATTERNS[@]}x${#PRED_TYPES[@]} queries in one process..."
    "$MINIGU" execute "$script_file" --path "$DB_PATH" 2>&1 | tee "$stdout_file" || true

    # Parse results: each query produces a line like:
    #   L1_PA, cardinality: 550233422
    # followed by a Time: line from :time
    # Extract all (pattern_name, cardinality) and Time lines in order

    cardinalities=()
    while IFS= read -r line; do
        cardinalities+=("$line")
    done < <(grep 'cardinality:' "$stdout_file" | grep -o '[^,]*, cardinality: [0-9]*')

    times=()
    while IFS= read -r line; do
        times+=("$line")
    done < <(grep -o 'Time: [0-9.]*s' "$stdout_file" | grep -o '[0-9.]*')

    # Map results back to (pattern, pred_type) order
    idx=0
    for pattern_path in "${PATTERNS[@]}"; do
        rel_path="${pattern_path#$PATTERN_DIR/}"
        group="$(dirname "$rel_path")"
        pattern="$(basename "$rel_path" .json)"

        for i in "${!PRED_TYPES[@]}"; do
            pred_type="${PRED_TYPES[$i]}"
            pred_name="${PRED_NAMES[$i]}"

            cardinality=0
            time_s=0

            if [[ $idx -lt ${#cardinalities[@]} ]]; then
                cardinality=$(echo "${cardinalities[$idx]}" | grep -o 'cardinality: [0-9]*' | awk '{print $2}') || cardinality=0
            fi
            if [[ $idx -lt ${#times[@]} ]]; then
                time_s="${times[$idx]}"
            fi

            echo "$SF,$group,$pattern,$pred_type,$pred_name,$cardinality,$time_s" >> "$OUTPUT_CSV"
            log "  $group/$pattern pred=$pred_name -> cardinality=$cardinality time=${time_s}s"

            idx=$((idx + 1))
        done
    done
done

log "Benchmark complete. Results saved to $OUTPUT_CSV"
echo ""
echo "=== Results ==="
column -t -s',' "$OUTPUT_CSV"
