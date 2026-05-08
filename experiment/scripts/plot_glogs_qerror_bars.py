#!/usr/bin/env python3
import argparse
import csv
import math
import re
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

from plot_qerror_bars import (
    BASELINE_COLORS,
    normalize_query_name,
    parse_estimate_file,
    parse_truth_file,
    qerror,
    query_sort_key,
)


BASELINES = ["gcard", "pathce", "gcare", "color", "safebound", "bayescard"]
BASELINE_COLORS = {
    **BASELINE_COLORS,
    "bayescard": BASELINE_COLORS["factorjoin"],
}

DEFAULT_ESTIMATE_PATHS = {
    "gcard": "results/gcard/estimate/glogs_sf1_k3_d0.log",
    "pathce": "results/pathce/estimate/glogs_sf1_k3_d0.log",
    "gcare": "results/gcare/estimate/glogs_sf1_wj.log",
    "color": "results/color/estimate/glogs_sf1.log",
    "safebound": "results/safebound/estimate/glogs_sf1.csv",
    "bayescard": "results/bayescard/glogs_sf1.csv",
}


def parse_bayescard_estimates(path: Path) -> dict:
    rows = {}
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        reader = csv.DictReader(f)
        for row in reader:
            q = row.get("query_file")
            est = row.get("prediction")
            if not q or not est:
                continue
            try:
                rows[q.strip()] = float(est)
            except ValueError:
                continue
    return rows


def parse_estimates(baseline: str, path: Path) -> dict:
    if baseline == "bayescard":
        return parse_bayescard_estimates(path)
    return parse_estimate_file(path)


def qerror_per_pattern(est_map: dict, truth_map: dict) -> dict:
    out = {}
    for q, est in est_map.items():
        nq = normalize_query_name(q)
        if nq in truth_map:
            out[nq] = qerror(est, truth_map[nq])
    return out


def glogs_sort_key(name: str):
    m = re.match(r"^p(\d+)$", name)
    if m:
        return (0, int(m.group(1)))
    return query_sort_key(name)


def plot_pattern_bars(baseline_to_qe: dict, output_path: Path):
    pattern_set = set()
    for qmap in baseline_to_qe.values():
        pattern_set.update(qmap.keys())
    patterns = sorted(pattern_set, key=glogs_sort_key)
    if not patterns:
        raise RuntimeError("No Glogs q-error data to plot")

    x = np.arange(len(patterns))
    width = 0.12
    fig, ax = plt.subplots(figsize=(12, 5.2), dpi=180)

    for i, baseline in enumerate(BASELINES):
        qmap = baseline_to_qe.get(baseline, {})
        vals = [qmap.get(p, float("nan")) for p in patterns]
        offset = (i - (len(BASELINES) - 1) / 2) * width
        ax.bar(
            x + offset,
            vals,
            width,
            label=baseline,
            color=BASELINE_COLORS[baseline],
            edgecolor="none",
            linewidth=0.0,
        )

    ax.set_xticks(x)
    ax.set_xticklabels(patterns, rotation=45, ha="right", fontsize=9)
    ax.set_ylabel("Q-Error", fontsize=11)
    ax.set_xlabel("Pattern", fontsize=11)
    ax.set_yscale("log")
    ax.grid(axis="y", linestyle="--", linewidth=0.6, alpha=0.5)
    ax.legend(ncol=3, fontsize=9, frameon=False, loc="upper left")
    ax.set_title("Glogs: per-pattern Q-Error", fontsize=12)
    plt.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(output_path)
    plt.close(fig)
    print(f"[OK] Saved figure to: {output_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Plot Glogs per-pattern Q-Error bars for six baselines."
    )
    parser.add_argument(
        "--workspace",
        default=str(Path(__file__).resolve().parents[1]),
        help="experiment workspace path",
    )
    parser.add_argument(
        "--truth",
        default="results/true_card/glogs/true_card.csv",
        help="Glogs true cardinality file, relative to workspace unless absolute",
    )
    parser.add_argument(
        "--output",
        default="results/qerror_glogs_per_pattern.png",
        help="output image path, relative to workspace unless absolute",
    )
    args = parser.parse_args()

    ws = Path(args.workspace).resolve()
    truth_path = Path(args.truth)
    if not truth_path.is_absolute():
        truth_path = ws / truth_path
    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = ws / output_path

    truth_map = {
        normalize_query_name(k): v for k, v in parse_truth_file(truth_path).items()
    }
    print(f"[INFO] loaded {len(truth_map)} truth entries from {truth_path}")

    baseline_to_qe = {}
    for baseline in BASELINES:
        estimate_path = ws / DEFAULT_ESTIMATE_PATHS[baseline]
        if not estimate_path.exists():
            print(f"[WARN] missing estimate file, skip: {estimate_path}")
            baseline_to_qe[baseline] = {}
            continue
        est_map = parse_estimates(baseline, estimate_path)
        qe_map = qerror_per_pattern(est_map, truth_map)
        baseline_to_qe[baseline] = qe_map
        finite_vals = [v for v in qe_map.values() if math.isfinite(v)]
        print(
            f"[INFO] {baseline:>10} | matched={len(qe_map)} "
            f"| median={np.median(finite_vals):.4g}" if finite_vals else
            f"[INFO] {baseline:>10} | matched=0"
        )

    plot_pattern_bars(baseline_to_qe, output_path)


if __name__ == "__main__":
    main()
