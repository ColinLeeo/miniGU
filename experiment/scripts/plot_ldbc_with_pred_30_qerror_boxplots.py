#!/usr/bin/env python3
import csv
import math
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.patches import Patch


BASELINE_COLORS = {
    "gcard": "#e74c3c",
    "safebound": "#9b59b6",
    "bayescard": "#7f8c8d",
}

BASELINES = ["gcard", "safebound", "bayescard"]
PATTERNS = ["L1_PA", "L2_PB", "L3_PB", "L4_PA", "L5_PB", "L6_PA"]

INPUTS = {
    "bayescard": Path(
        "/home/zxz/miniGU/experiment/baseline/SafeBound/"
        "repro_bayescard_ldbc_sf1/results/"
        "ldbc_with_pred_30_qerror_paper.csv"
    ),
    "safebound": Path(
        "/home/zxz/miniGU/experiment/results/safebound/estimate/"
        "ldbc_with_pred_30_sf1_qerror_floor1.csv"
    ),
    "gcard": Path(
        "/home/zxz/miniGU/experiment/results/gcard/estimate/"
        "ldbc_with_pred_30_sf1_k3_p0_d0_qerror_floor1.csv"
    ),
}

OUT_DIR = Path("/home/zxz/miniGU/experiment/results/plots")
OUT_STEM = OUT_DIR / "ldbc_with_pred_30_qerror_boxplots"


def _float_or_none(raw):
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _fallback_inf_qerror(row):
    """Replace infinite q-error with the non-zero side after floor-1 semantics."""
    true_card = _float_or_none(
        row.get("true_cardinality") or row.get("raw_true_cardinality")
    )
    prediction = _float_or_none(row.get("prediction") or row.get("raw_prediction"))
    candidates = []
    if true_card is not None:
        candidates.append(max(true_card, 1.0))
    if prediction is not None:
        candidates.append(max(prediction, 1.0))
    return max(candidates) if candidates else None


def read_qerrors(path):
    values = {pattern: [] for pattern in PATTERNS}
    inf_replaced_counts = {pattern: 0 for pattern in PATTERNS}
    failure_counts = {pattern: 0 for pattern in PATTERNS}

    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            pattern = row.get("base_pattern") or row.get("base")
            if pattern not in values:
                continue

            status = str(row.get("status", "")).strip().lower()
            if status.startswith("failed") or status.startswith("error"):
                failure_counts[pattern] += 1
                continue

            raw = str(row.get("q_error", "")).strip().lower()
            if raw in {"inf", "infinity"}:
                qerror = _fallback_inf_qerror(row)
                if qerror is not None and qerror > 0:
                    values[pattern].append(qerror)
                    inf_replaced_counts[pattern] += 1
                continue

            try:
                qerror = float(raw)
            except ValueError:
                continue

            if math.isfinite(qerror):
                values[pattern].append(qerror)
            else:
                qerror = _fallback_inf_qerror(row)
                if qerror is not None and qerror > 0:
                    values[pattern].append(qerror)
                    inf_replaced_counts[pattern] += 1

    return values, inf_replaced_counts, failure_counts


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    data = {}
    inf_replaced_counts = {}
    failure_counts = {}
    for baseline, path in INPUTS.items():
        (
            data[baseline],
            inf_replaced_counts[baseline],
            failure_counts[baseline],
        ) = read_qerrors(path)

    fig, ax = plt.subplots(figsize=(12.5, 4.8))
    offsets = {"gcard": -0.24, "safebound": 0.0, "bayescard": 0.24}
    width = 0.18

    for i, pattern in enumerate(PATTERNS, start=1):
        for baseline in BASELINES:
            position = i + offsets[baseline]
            vals = data[baseline][pattern]
            if vals:
                bp = ax.boxplot(
                    vals,
                    positions=[position],
                    widths=width,
                    patch_artist=True,
                    showfliers=False,
                    medianprops={"color": "black", "linewidth": 1.2},
                    boxprops={"linewidth": 1.0},
                    whiskerprops={"linewidth": 1.0},
                    capprops={"linewidth": 1.0},
                )
                for box in bp["boxes"]:
                    box.set_facecolor(BASELINE_COLORS[baseline])
                    box.set_alpha(0.78)
                    box.set_edgecolor("#333333")

            labels = []
            inf_count = inf_replaced_counts[baseline][pattern]
            fail_count = failure_counts[baseline][pattern]
            if inf_count:
                labels.append(f"+{inf_count} inf->finite")
            if fail_count:
                labels.append(f"+{fail_count} failed")
            if labels:
                y = max(vals) * 1.35 if vals else 1.2
                ax.text(
                    position,
                    y,
                    "\n".join(labels),
                    ha="center",
                    va="bottom",
                    fontsize=7,
                    rotation=90,
                    color=BASELINE_COLORS[baseline],
                )

    ax.set_yscale("log")
    ax.set_ylabel("Q-error (log scale)")
    ax.set_xlabel("Pattern")
    ax.set_xticks(range(1, len(PATTERNS) + 1))
    ax.set_xticklabels(PATTERNS)
    ax.set_title("LDBC SF1 with predicates: Q-error by pattern")
    ax.grid(True, axis="y", which="both", linestyle="--", linewidth=0.5, alpha=0.45)
    ax.set_axisbelow(True)

    legend = [
        Patch(facecolor=BASELINE_COLORS[name], edgecolor="#333333", label=name)
        for name in BASELINES
    ]
    ax.legend(handles=legend, loc="upper left", ncol=3, frameon=False)

    fig.tight_layout()
    fig.savefig(f"{OUT_STEM}.png", dpi=300)
    fig.savefig(f"{OUT_STEM}.pdf")
    print(f"{OUT_STEM}.png")
    print(f"{OUT_STEM}.pdf")


if __name__ == "__main__":
    main()
