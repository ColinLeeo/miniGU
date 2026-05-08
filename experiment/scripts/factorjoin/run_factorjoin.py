#!/usr/bin/env python3
import argparse
import glob
import os
import pickle
import re
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd


def add_factorjoin_to_path(workspace: Path) -> None:
    root = workspace / "baseline" / "FactorJoin"
    sys.path.insert(0, str(root))


def parse_queries_with_factorjoin_parser(sql_files):
    from Join_scheme.join_graph import parse_query_simple

    all_tables = set()
    join_columns = {}
    filter_columns = {}
    relationships = set()

    for sql_file in sql_files:
        query = sql_file.read_text(encoding="utf-8").strip()
        tables_all, table_cond, join_cond, _ = parse_query_simple(query)

        for table in set(tables_all.values()):
            all_tables.add(table)
            join_columns.setdefault(table, set())
            filter_columns.setdefault(table, set())

        for table, conds in table_cond.items():
            filter_columns.setdefault(table, set())
            for attr, _, _ in conds:
                if "." not in attr:
                    continue
                filter_columns[table].add(attr.split(".", 1)[1])

        for cond in join_cond:
            # cond format after parse_query_simple: "table.col = table.col"
            left, right = [x.strip() for x in cond.split("=")]
            l_table, l_col = left.split(".", 1)
            r_table, r_col = right.split(".", 1)
            all_tables.add(l_table)
            all_tables.add(r_table)
            join_columns.setdefault(l_table, set()).add(l_col)
            join_columns.setdefault(r_table, set()).add(r_col)
            relationships.add((l_table, l_col, r_table, r_col))

    return all_tables, join_columns, filter_columns, sorted(relationships)


def locate_csv_for_table(dataset_dir: Path, table_name: str) -> Path:
    table_lower = table_name.lower()
    for path in dataset_dir.glob("*.csv"):
        if path.stem.lower() == table_lower:
            return path
    raise FileNotFoundError(f"Cannot find CSV for table '{table_name}' in {dataset_dir}")


def read_table_with_needed_columns(csv_path: Path, table_name: str, needed_cols: set[str]) -> pd.DataFrame:
    raw = pd.read_csv(csv_path, low_memory=False)
    col_map = {c.lower(): c for c in raw.columns}

    selected_actual = []
    for c in sorted(needed_cols):
        key = c.lower()
        if key not in col_map:
            raise KeyError(
                f"Column '{c}' required by workload not found in {csv_path}. "
                f"Available: {list(raw.columns)}"
            )
        selected_actual.append(col_map[key])

    # preserve deterministic order and remove dup
    dedup = []
    seen = set()
    for c in selected_actual:
        if c not in seen:
            dedup.append(c)
            seen.add(c)

    df = raw[dedup].copy()
    df.columns = [f"{table_name}.{c}" for c in dedup]
    return df


def build_schema_and_data(dataset_dir: Path, sql_files):
    from Schemas.graph_representation import SchemaGraph, Table

    all_tables, join_cols, filter_cols, rels = parse_queries_with_factorjoin_parser(sql_files)

    schema = SchemaGraph()
    data = {}
    key_attrs = {}
    null_values = {}

    # Build Table objects first
    for table in sorted(all_tables):
        jc = join_cols.get(table, set())
        fc = filter_cols.get(table, set())
        needed = set(jc) | set(fc)
        if not needed:
            needed = {"id"}

        csv_path = locate_csv_for_table(dataset_dir, table)
        df = read_table_with_needed_columns(csv_path, table, needed)

        attrs = [c.split(".", 1)[1] for c in df.columns]
        pk = ["id"] if "id" in [a.lower() for a in attrs] else [attrs[0]]
        table_obj = Table(
            table_name=table,
            primary_key=pk,
            attributes=attrs,
            irrelevant_attributes=[],
            csv_file_location=str(csv_path),
            table_size=len(df),
        )
        schema.add_table(table_obj)

        data[table] = df
        key_attrs[table] = sorted([f"{table}.{c}" for c in jc])
        null_values[table] = {}

    # Build relationships
    seen_rel = set()
    for l_table, l_col, r_table, r_col in rels:
        key = (l_table.lower(), l_col.lower(), r_table.lower(), r_col.lower())
        if key in seen_rel:
            continue
        seen_rel.add(key)
        schema.add_relationship(l_table, l_col, r_table, r_col)

    return schema, data, key_attrs, null_values


def prepare_buckets(schema, data, key_attrs, null_values, n_bins: int, bucket_method: str):
    from Join_scheme.binning import (
        Table_bucket,
        get_start_key,
        greedy_bucketize,
        identify_key_values,
        naive_bucketize,
        sub_optimal_bucketize,
    )
    from Join_scheme.data_prepare import generate_table_buckets

    all_keys, equivalent_keys = identify_key_values(schema)
    if not equivalent_keys:
        raise RuntimeError("No join-key groups found from queries. Cannot train FactorJoin.")

    key_data = {}
    sample_rate = {}
    table_key_lens = {}
    primary_keys = []

    for table in data:
        for full_col in key_attrs.get(table, []):
            if full_col not in all_keys:
                continue
            arr = pd.to_numeric(data[table][full_col], errors="coerce").to_numpy(copy=True)
            arr[np.isnan(arr)] = -1
            arr[arr < 0] = -1
            arr = arr + 1  # follow FactorJoin convention for null key handling
            key_data[full_col] = arr[arr > 0]
            table_key_lens[full_col] = len(arr)
            sample_rate[full_col] = 1.0
            null_values[table][full_col] = 0
            if len(key_data[full_col]) > 0 and len(np.unique(key_data[full_col])) >= len(key_data[full_col]) * 0.99:
                primary_keys.append(full_col)

        for col in data[table].columns:
            if col in key_attrs.get(table, []):
                continue
            vals = pd.to_numeric(data[table][col], errors="coerce").to_numpy(copy=True)
            finite = vals[~np.isnan(vals)]
            null_values[table][col] = float(np.min(finite) - 100) if len(finite) > 0 else -100.0

    binned_data = {}
    optimal_buckets = {}
    all_bin_modes = {}
    bin_size = {}

    for pk in equivalent_keys:
        group_data = {}
        group_sample = {}
        for k in equivalent_keys[pk]:
            if k not in key_data:
                raise KeyError(f"Join key '{k}' not found in data columns; check query/data consistency.")
            group_data[k] = key_data[k]
            group_sample[k] = sample_rate[k]

        if bucket_method == "greedy":
            temp_data, optimal_bucket = greedy_bucketize(group_data, group_sample, n_bins, primary_keys, True)
        elif bucket_method == "sub_optimal":
            temp_data, optimal_bucket = sub_optimal_bucketize(group_data, group_sample, n_bins, primary_keys, False, True)
        elif bucket_method == "naive":
            temp_data, optimal_bucket = naive_bucketize(group_data, group_sample, n_bins, primary_keys, True)
        elif bucket_method == "fixed_start_key":
            start_key = get_start_key(list(group_data.keys()), table_key_lens, primary_keys)
            temp_data, optimal_bucket = sub_optimal_bucketize(group_data, group_sample, n_bins, primary_keys, False, True)
            # Keep behavior deterministic and compatible; fixed_start_key path is expensive and requires per-key bin map.
            # sub_optimal provides a stable fallback for generic datasets.
            _ = start_key
        else:
            raise ValueError(f"Unsupported bucket_method: {bucket_method}")

        binned_data.update(temp_data)
        for k in equivalent_keys[pk]:
            optimal_buckets[k] = optimal_bucket
            t = k.split(".", 1)[0]
            if t not in bin_size:
                bin_size[t] = {}
            bin_size[t][k] = len(optimal_bucket.bins)
            all_bin_modes[k] = np.asarray(optimal_bucket.buckets[k].bin_modes)

    # Apply binned key values back to table data
    for k, vals in binned_data.items():
        t = k.split(".", 1)[0]
        raw_vals = pd.to_numeric(data[t][k], errors="coerce").to_numpy(copy=True)
        raw_vals[np.isnan(raw_vals)] = -1
        raw_vals[raw_vals < 0] = -1
        raw_vals = raw_vals + 1
        mask = raw_vals > 0
        # binned_data is built from non-null key rows, in order
        raw_vals[mask] = vals
        data[t][k] = raw_vals

    table_buckets = generate_table_buckets(data, {k: data[k.split('.', 1)[0]][k].to_numpy() for k in binned_data}, key_attrs, bin_size, all_bin_modes, optimal_buckets)
    return table_buckets, equivalent_keys, bin_size, null_values


def train_model(args):
    workspace = Path(args.workspace).resolve()
    add_factorjoin_to_path(workspace)

    from BayesCard.Models.Bayescard_BN import Bayescard_BN
    from Join_scheme.bound import Bound_ensemble

    dataset_dir = Path(args.dataset_dir).resolve()
    pattern_dir = Path(args.pattern_dir).resolve()
    output_path = Path(args.output_model).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    sql_files = sorted(Path(p) for p in glob.glob(str(pattern_dir / "**" / "*.sql"), recursive=True))
    if not sql_files:
        raise FileNotFoundError(f"No SQL files found under {pattern_dir}")

    schema, data, key_attrs, null_values = build_schema_and_data(dataset_dir, sql_files)
    table_buckets, _, bin_size, null_values = prepare_buckets(
        schema, data, key_attrs, null_values, args.n_bins, args.bucket_method
    )

    all_bns = {}
    for table in sorted(data.keys()):
        if table not in bin_size:
            # table might only appear with predicates, no join key; skip for FactorJoin core path.
            continue
        bn = Bayescard_BN(table, key_attrs[table], bin_size[table], null_values=null_values[table])
        bn.build_from_data(data[table])
        all_bns[table] = bn

    start = time.perf_counter()
    be = Bound_ensemble(table_buckets, schema, args.n_dim_dist, bns=all_bns, null_value=null_values)
    build_time = time.perf_counter() - start

    with open(output_path, "wb") as f:
        pickle.dump(be, f, pickle.HIGHEST_PROTOCOL)

    print(f"Saved model: {output_path}")
    print(f"Loaded tables: {len(data)}, trained BNs: {len(all_bns)}")
    print(f"Bound ensemble build time: {build_time:.6f} s")


def estimate_model(args):
    workspace = Path(args.workspace).resolve()
    add_factorjoin_to_path(workspace)

    pattern_dir = Path(args.pattern_dir).resolve()
    model_path = Path(args.model_path).resolve()
    output_log = Path(args.output_log).resolve()
    output_log.parent.mkdir(parents=True, exist_ok=True)

    with open(model_path, "rb") as f:
        bound_ensemble = pickle.load(f)

    if bound_ensemble.bns is not None:
        for bn in bound_ensemble.bns.values():
            if getattr(bn, "infer_machine", None) is None:
                bn.init_inference_method()

    sql_files = sorted(Path(p) for p in glob.glob(str(pattern_dir / "**" / "*.sql"), recursive=True))
    if not sql_files:
        raise FileNotFoundError(f"No SQL files found under {pattern_dir}")

    with open(output_log, "w", encoding="utf-8") as f:
        for sql_file in sql_files:
            query = sql_file.read_text(encoding="utf-8").strip()
            start = time.perf_counter()
            est = bound_ensemble.get_cardinality_bound_one(query, sql_file.name)
            latency = time.perf_counter() - start
            rel = sql_file.relative_to(pattern_dir).as_posix()
            line = f"{rel}: {est}, {latency:.6f}"
            print(line)
            f.write(line + "\n")

    print(f"Log: {output_log}")
    print(f"Queries estimated: {len(sql_files)}")


def main():
    parser = argparse.ArgumentParser(description="FactorJoin build/estimate helper for miniGU datasets.")
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="Build FactorJoin model.")
    build.add_argument("--workspace", required=True)
    build.add_argument("--dataset-dir", required=True)
    build.add_argument("--pattern-dir", required=True)
    build.add_argument("--output-model", required=True)
    build.add_argument("--n-dim-dist", type=int, default=2)
    build.add_argument("--n-bins", type=int, default=200)
    build.add_argument("--bucket-method", default="greedy", choices=["greedy", "sub_optimal", "naive", "fixed_start_key"])

    estimate = sub.add_parser("estimate", help="Estimate all queries by FactorJoin model.")
    estimate.add_argument("--workspace", required=True)
    estimate.add_argument("--pattern-dir", required=True)
    estimate.add_argument("--model-path", required=True)
    estimate.add_argument("--output-log", required=True)

    args = parser.parse_args()
    if args.command == "build":
        train_model(args)
    else:
        estimate_model(args)


if __name__ == "__main__":
    main()
