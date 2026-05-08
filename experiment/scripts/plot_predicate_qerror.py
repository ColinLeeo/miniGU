#!/usr/bin/env python3
import csv
import math
import re
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np


METHODS = ["gcard_p0", "safebound"]
COLORS = {
    "gcard_p0": "#e74c3c",
    "safebound": "#9b59b6",
}


def normalize(name: str) -> str:
    name = name.strip().replace("\\", "/").split("/")[-1]
    return re.sub(r"\.(json|sql|txt)$", "", name, flags=re.IGNORECASE)


def qerror(est: float, truth: float) -> float:
    e = max(float(est), 1.0)
    t = max(float(truth), 1.0)
    return max(e / t, t / e)


def read_truth_dir(path: Path) -> dict[str, float]:
    out = {}
    for f in sorted(path.glob("*.txt")):
        text = f.read_text(encoding="utf-8", errors="ignore").strip()
        if not text:
            continue
        out[normalize(f.name)] = float(text.splitlines()[0].strip().split()[0])
    return out


def read_gcard_log(path: Path) -> dict[str, float]:
    out = {}
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = s.split()
        if len(parts) >= 2:
            out[normalize(parts[0])] = float(parts[1])
    return out


def read_safebound_csv(path: Path) -> dict[str, float]:
    out = {}
    with path.open("r", encoding="utf-8", errors="ignore", newline="") as f:
        for row in csv.DictReader(f):
            if not row:
                continue
            q = row.get("query")
            est = row.get("estimate")
            if q and est:
                out[normalize(q)] = float(est)
    return out


def compute_qerrors(estimates: dict[str, float], truth: dict[str, float]) -> dict[str, float]:
    return {
        q: qerror(est, truth[q])
        for q, est in estimates.items()
        if q in truth and math.isfinite(float(est))
    }


def plot_ldbc(qe_by_method: dict[str, dict[str, float]], output: Path) -> None:
    queries = sorted({q for vals in qe_by_method.values() for q in vals})
    x = np.arange(len(queries))
    width = 0.32

    fig, ax = plt.subplots(figsize=(9.5, 4.8), dpi=180)
    for i, method in enumerate(METHODS):
        vals = [qe_by_method.get(method, {}).get(q, np.nan) for q in queries]
        offset = (i - (len(METHODS) - 1) / 2) * width
        ax.bar(x + offset, vals, width, label=method, color=COLORS[method], edgecolor="none")

    ax.set_xticks(x)
    ax.set_xticklabels(queries, rotation=30, ha="right")
    ax.set_yscale("log")
    ax.set_ylabel("Q-Error")
    ax.set_xlabel("LDBC with predicate query")
    ax.set_title("LDBC with Predicate: Per-query Q-Error")
    ax.grid(axis="y", linestyle="--", linewidth=0.6, alpha=0.45)
    ax.legend(frameon=False, ncol=len(METHODS))
    fig.tight_layout()
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output)
    plt.close(fig)
    print(f"saved {output}")


def plot_stats_box(qe_by_method: dict[str, dict[str, float]], output: Path) -> None:
    data = [[qe_by_method[m][q] for q in sorted(qe_by_method[m])] for m in METHODS]

    fig, ax = plt.subplots(figsize=(7.2, 4.8), dpi=180)
    bp = ax.boxplot(data, tick_labels=METHODS, patch_artist=True, showfliers=False)
    for patch, method in zip(bp["boxes"], METHODS):
        patch.set_facecolor(COLORS[method])
        patch.set_edgecolor(COLORS[method])
        patch.set_alpha(0.9)
    for key in ["whiskers", "caps", "medians"]:
        for artist in bp[key]:
            artist.set_color("#222222")
            artist.set_linewidth(1.0)

    means = [float(np.mean(vals)) if vals else float("nan") for vals in data]
    ax.scatter(np.arange(1, len(METHODS) + 1), means, color="#111111", s=18, zorder=3, label="mean")
    for i, mean in enumerate(means, 1):
        if math.isfinite(mean):
            ax.text(i, mean * 1.08, f"mean={mean:.2g}", ha="center", va="bottom", fontsize=8)

    ax.set_yscale("log")
    ax.set_ylabel("Q-Error")
    ax.set_xlabel("Estimator")
    ax.set_title("STATS CEB: Q-Error Distribution")
    ax.grid(axis="y", linestyle="--", linewidth=0.6, alpha=0.45)
    ax.legend(frameon=False)
    fig.tight_layout()
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output)
    plt.close(fig)
    print(f"saved {output}")


def main() -> None:
    ws = Path(__file__).resolve().parents[1]

    ldbc_truth = read_truth_dir(ws / "results/true_card/ldbc_with_pred")
    ldbc_est = {
        "gcard_p0": read_gcard_log(ws / "results/gcard/estimate/ldbc_with_pred_sf1_k3_d0.log"),
        "safebound": read_safebound_csv(ws / "results/safebound/estimate/ldbc_with_pred_sf1.csv"),
    }
    ldbc_qe = {m: compute_qerrors(v, ldbc_truth) for m, v in ldbc_est.items()}
    for m in METHODS:
        print(f"ldbc_with_pred {m}: {len(ldbc_qe[m])} queries")
    plot_ldbc(ldbc_qe, ws / "results/qerror_ldbc_with_pred_per_query.png")

    stats_truth = read_truth_dir(ws / "results/true_card/stats")
    stats_est = {
        "gcard_p0": read_gcard_log(ws / "results/gcard/estimate/stats_ceb_k3_d0.log"),
        "safebound": read_safebound_csv(ws / "results/safebound/estimate/stats_ceb.csv"),
    }
    stats_qe = {m: compute_qerrors(v, stats_truth) for m, v in stats_est.items()}
    for m in METHODS:
        vals = list(stats_qe[m].values())
        print(f"stats_ceb {m}: {len(vals)} queries, mean={np.mean(vals):.6g}, median={np.median(vals):.6g}")
    plot_stats_box(stats_qe, ws / "results/qerror_stats_ceb_box.png")


if __name__ == "__main__":
    main()
