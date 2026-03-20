#!/usr/bin/env python3
"""
Compute true cardinality of a graph pattern query using DuckDB.

Reads a query JSON (same format as miniGU's GCard queries) and translates it
into a SQL JOIN query over the LDBC CSV files.

Usage:
    python3 true_cardinality.py <data_dir> <query.json> [query2.json ...]
    python3 true_cardinality.py <data_dir> --all          # run all patterns

Example:
    python3 true_cardinality.py experiment/dataset/ldbc/sf1 experiment/pattern/LDBC/L4_cycle/L4.json
"""

import json
import sys
import os
import glob
import time

try:
    import duckdb
except ImportError:
    print("pip install duckdb", file=sys.stderr)
    sys.exit(1)


OP_MAP = {
    "eq": "=",
    "ne": "!=",
    "gt": ">",
    "ge": ">=",
    "lt": "<",
    "le": "<=",
}


def load_query(path):
    with open(path) as f:
        return json.load(f)


def query_to_sql(query, data_dir):
    """Convert a query JSON to a DuckDB SQL COUNT(*) query."""
    vertices = {v["id"]: v["label"] for v in query["vertices"]}
    edges = query["edges"]
    predicates = query.get("predicates", [])

    # For each query vertex, track which (edge_alias, column) references it.
    # This lets us build JOIN conditions and predicate joins.
    vertex_refs = {}  # vertex_id -> (edge_alias, "src"|"dst")
    for e in edges:
        alias = f"e{e['id']}"
        for side in ("src", "dst"):
            vid = e[side]
            if vid not in vertex_refs:
                vertex_refs[vid] = (alias, side)

    # Build FROM / JOIN clauses
    from_parts = []
    join_conditions = []

    # Track which vertex_id has been "pinned" to which expression
    vertex_expr = {}  # vertex_id -> SQL expression (e.g., "e1.src")

    for i, e in enumerate(edges):
        alias = f"e{e['id']}"
        csv_file = os.path.join(data_dir, f"{e['label']}.csv")
        table_expr = f"read_csv('{csv_file}') {alias}"

        if i == 0:
            from_parts.append(table_expr)
            # Pin src and dst vertices
            vertex_expr[e["src"]] = f"{alias}.src"
            vertex_expr[e["dst"]] = f"{alias}.dst"
        else:
            # Build ON conditions: for src and dst, if the vertex was already
            # pinned by a previous edge, add an equality condition.
            on_conds = []
            for side in ("src", "dst"):
                vid = e[side]
                col_expr = f"{alias}.{side}"
                if vid in vertex_expr:
                    on_conds.append(f"{col_expr} = {vertex_expr[vid]}")
                else:
                    vertex_expr[vid] = col_expr

            if on_conds:
                from_parts.append(f"JOIN {table_expr} ON {' AND '.join(on_conds)}")
            else:
                # Cartesian product (disconnected component) - shouldn't happen
                # in well-formed queries, but handle gracefully
                from_parts.append(f"CROSS JOIN {table_expr}")

    # Handle vertex predicates: need to join vertex tables for property access
    where_parts = []
    vertex_table_joined = {}  # vertex_id -> table alias

    for pred in predicates:
        if pred["target"] == "vertex":
            vid = pred["id"]
            label = vertices[vid]
            prop = pred["property"]
            op = OP_MAP[pred["op"]]

            # Parse value
            val = pred["value"]
            if isinstance(val, dict):
                if "String" in val:
                    val_sql = f"'{val['String']}'"
                elif "Int64" in val:
                    val_sql = str(val["Int64"])
                elif "Float64" in val:
                    val_sql = str(val["Float64"])
                elif "Boolean" in val:
                    val_sql = "true" if val["Boolean"] else "false"
                else:
                    val_sql = str(list(val.values())[0])
            else:
                val_sql = f"'{val}'" if isinstance(val, str) else str(val)

            # Join vertex table if not already joined
            if vid not in vertex_table_joined:
                v_alias = f"v{vid}"
                csv_file = os.path.join(data_dir, f"{label}.csv")
                pin_expr = vertex_expr[vid]
                from_parts.append(
                    f"JOIN read_csv('{csv_file}') {v_alias} ON {v_alias}.id = {pin_expr}"
                )
                vertex_table_joined[vid] = v_alias

            v_alias = vertex_table_joined[vid]
            where_parts.append(f"{v_alias}.{prop} {op} {val_sql}")

        elif pred["target"] == "edge":
            eid = pred["id"]
            alias = f"e{eid}"
            prop = pred["property"]
            op = OP_MAP[pred["op"]]
            val = pred["value"]
            if isinstance(val, dict):
                if "String" in val:
                    val_sql = f"'{val['String']}'"
                elif "Int64" in val:
                    val_sql = str(val["Int64"])
                else:
                    val_sql = str(list(val.values())[0])
            else:
                val_sql = f"'{val}'" if isinstance(val, str) else str(val)
            where_parts.append(f"{alias}.{prop} {op} {val_sql}")

    sql = "SELECT COUNT(*) AS cardinality\nFROM " + "\n".join(from_parts)
    if where_parts:
        sql += "\nWHERE " + " AND ".join(where_parts)

    return sql


def run_query(data_dir, query_path):
    query = load_query(query_path)
    sql = query_to_sql(query, data_dir)
    name = os.path.splitext(os.path.basename(query_path))[0]

    con = duckdb.connect()
    t0 = time.time()
    result = con.execute(sql).fetchone()[0]
    elapsed = time.time() - t0
    con.close()

    print(f"{name}: {result}  ({elapsed:.2f}s)")
    return name, result, elapsed, sql


def main():
    if len(sys.argv) < 3:
        print(__doc__.strip())
        sys.exit(1)

    data_dir = sys.argv[1]
    if not os.path.isdir(data_dir):
        print(f"ERROR: data directory not found: {data_dir}", file=sys.stderr)
        sys.exit(1)

    if sys.argv[2] == "--all":
        pattern_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "pattern", "LDBC",
        )
        query_files = sorted(glob.glob(os.path.join(pattern_dir, "**/*.json"), recursive=True))
    else:
        query_files = sys.argv[2:]

    if not query_files:
        print("No query files found.", file=sys.stderr)
        sys.exit(1)

    results = []
    for qf in query_files:
        try:
            name, card, elapsed, sql = run_query(data_dir, qf)
            results.append((name, card, elapsed))
        except Exception as e:
            print(f"{os.path.basename(qf)}: ERROR - {e}", file=sys.stderr)

    if len(results) > 1:
        print("\n=== Summary ===")
        for name, card, elapsed in results:
            print(f"  {name:20s}  {card:>15,}  ({elapsed:.2f}s)")


if __name__ == "__main__":
    main()
