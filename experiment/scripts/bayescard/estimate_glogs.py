#!/usr/bin/env python3
import argparse
import csv
import importlib.util
import os
import re
import sys
import time
import traceback


WORKSPACE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
REPRO_DIR = os.path.join(
    WORKSPACE, "baseline", "SafeBound", "repro_bayescard_ldbc_sf1"
)
DEFAULT_QUERY_DIR = os.path.join(WORKSPACE, "pattern_sql", "glogs")
DEFAULT_MODEL_DIR = os.path.join(REPRO_DIR, "models_full_paper")
DEFAULT_OUTPUT = os.path.join(WORKSPACE, "results", "bayescard", "glogs_sf1.csv")


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def split_and(expr):
    return [
        part.strip()
        for part in re.split(r"(?i)\s+and\s+", expr.strip())
        if part.strip()
    ]


def normalize_table_name(table):
    return table.lower()


def parse_from_aliases(from_part):
    alias_dict = {}
    for table_expr in from_part.split(","):
        parts = table_expr.strip().split()
        if len(parts) == 1:
            table = parts[0]
            alias = table
        elif len(parts) == 2:
            table, alias = parts
        elif len(parts) == 3 and parts[1].lower() == "as":
            table, alias = parts[0], parts[2]
        else:
            raise ValueError("Unsupported FROM table expression: {}".format(table_expr))
        alias_dict[alias] = normalize_table_name(table)
    return alias_dict


def resolve_attr(token, alias_dict):
    token = token.strip()
    match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)$", token)
    if not match:
        raise ValueError("Expected qualified attribute, got {}".format(token))
    alias, attr = match.groups()
    if alias not in alias_dict:
        raise KeyError(alias)
    return alias_dict[alias], attr.lower()


def parse_glogs_sql(query_sql, schema, bayescard_eval):
    sql = " ".join(query_sql.strip().rstrip(";").split())
    match = re.match(r"(?is)^select\s+count\(\*\)\s+from\s+(.+?)(?:\s+where\s+(.+))?$", sql)
    if not match:
        raise ValueError("Only SELECT COUNT(*) FROM ... [WHERE ...] queries are supported")

    from_part, where_part = match.groups()
    alias_dict = parse_from_aliases(from_part)

    query = bayescard_eval.Query(schema)
    for table in alias_dict.values():
        query.table_set.add(table)

    if where_part:
        for condition in split_and(where_part):
            join_match = re.match(r"^(.+?)\s*=\s*(.+)$", condition)
            if join_match:
                left = join_match.group(1).strip()
                right = join_match.group(2).strip()
                if re.match(r"^[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*$", right):
                    bayescard_eval.add_join(query, schema, left, right, alias_dict)
                    continue

            pred_match = re.match(r"^(.+?)\s*(=|>=|<=|>|<)\s*(.+)$", condition)
            if not pred_match:
                raise ValueError("Unsupported WHERE condition: {}".format(condition))
            left, operator, value = pred_match.groups()
            table, attr = resolve_attr(left, alias_dict)
            query.add_where_condition(table, "{}{}{}".format(attr, operator, value.strip()))

    return query, alias_dict


def main():
    parser = argparse.ArgumentParser(
        description="Estimate Glogs SQL patterns with the BayesCard LDBC SF1 reproduction."
    )
    parser.add_argument("--query-dir", default=DEFAULT_QUERY_DIR)
    parser.add_argument("--model-dir", default=DEFAULT_MODEL_DIR)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--data-dir",
        default=os.path.join(WORKSPACE, "datasets", "ldbc", "sf1"),
    )
    args = parser.parse_args()

    eval_path = os.path.join(REPRO_DIR, "scripts", "06_eval_ldbc_with_pred.py")
    bayescard_eval = load_module("bayescard_ldbc_eval", eval_path)
    eval_full = bayescard_eval._load_eval_full_module()

    os.makedirs(os.path.dirname(args.output), exist_ok=True)

    schema = bayescard_eval.gen_ldbc_sf1_full_schema(args.data_dir)
    ensemble = eval_full.load_ensemble(schema, args.model_dir)
    supported_columns = bayescard_eval.supported_condition_columns(ensemble)

    sql_files = [
        f for f in sorted(os.listdir(args.query_dir)) if f.endswith(".sql")
    ]
    if not sql_files:
        raise FileNotFoundError("No .sql files found under {}".format(args.query_dir))

    latencies = []
    with open(args.output, "w", newline="") as out:
        writer = csv.writer(out)
        writer.writerow(
            [
                "query_file",
                "prediction",
                "latency_ms",
                "relationships",
                "where_conditions",
                "status",
                "notes",
                "mode",
                "ignored_conditions",
            ]
        )
        for file_name in sql_files:
            path = os.path.join(args.query_dir, file_name)
            query_sql = open(path, encoding="utf-8").read()
            tic = time.time()
            relationships = ""
            where_conditions = ""
            notes = ""
            mode = ""
            ignored_conditions = []
            try:
                query, aliases = parse_glogs_sql(query_sql, schema, bayescard_eval)
                notes = bayescard_eval.alias_caveat(aliases)
                relationships = ";".join(sorted(query.relationship_set))
                where_conditions = ";".join(
                    "{}:{}".format(table, "|".join(conds))
                    for table, conds in sorted(query.table_where_condition_dict.items())
                )
                prediction, mode, ignored_conditions, failures = (
                    bayescard_eval.estimate_query_with_fallbacks(
                        ensemble, query, supported_columns
                    )
                )
                if mode == "exact_relationships":
                    status = "ok"
                else:
                    status = "ok_approx"
                    if failures:
                        notes = ";".join([note for note in [notes] + failures if note])
                latencies.append((time.time() - tic) * 1000.0)
            except Exception as exc:
                prediction = ""
                status = "failed: {}: {}".format(type(exc).__name__, exc)
                print(traceback.format_exc())
                latencies.append((time.time() - tic) * 1000.0)

            latency_ms = latencies[-1]
            writer.writerow(
                [
                    file_name,
                    prediction,
                    latency_ms,
                    relationships,
                    where_conditions,
                    status,
                    notes,
                    mode,
                    "|".join(ignored_conditions),
                ]
            )
            print(
                "{} pred={} latency_ms={} status={} mode={} ignored={} notes={}".format(
                    file_name,
                    prediction,
                    latency_ms,
                    status,
                    mode,
                    "|".join(ignored_conditions),
                    notes,
                )
            )

    print("results written to {}".format(args.output))


if __name__ == "__main__":
    main()
