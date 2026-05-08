#!/usr/bin/env python3
import argparse
import csv
import math
import re
from collections import defaultdict
from pathlib import Path
from statistics import median

import matplotlib.pyplot as plt
import numpy as np


BASELINES = ["gcard", "pathce", "gcare", "color", "safebound", "factorjoin"]
DATASETS = ["lsqb", "imdb", "aids"]

# Extracted from experiment/results/image.png dominant bar colors.
BASELINE_COLORS = {
    "gcard": "#e74c3c",
    "pathce": "#3498db",
    "gcare": "#2ecc71",
    "color": "#f39c12",
    "safebound": "#9b59b6",
    "factorjoin": "#7f8c8d",
}

DEFAULT_ESTIMATE_PATHS = {
    "lsqb": {
        "color": "results/color/estimate/lsqb_sf1_maxmix.log",
        "gcare": "results/gcare/estimate/lsqb_sf1_wj.log",
        "gcard": "results/gcard/estimate/lsqb_sf1_k3_d0.log",
        "pathce": "results/pathce/estimate/lsqb_sf1_k3_d0.log",
        "safebound": "results/safebound/estimate/lsqb_sf1.csv",
        "factorjoin": "results/factorjoin/estimate/lsqb_sf1.log",
    },
    "imdb": {
        "color": "results/color/estimate/imdb.log",
        "gcare": "results/gcare/estimate/imdb_wj.log",
        "gcard": "results/gcard/estimate/imdb_k3_d0.log",
        "pathce": "results/pathce/estimate/imdb_k3_d0.log",
        "safebound": "results/safebound/estimate/imdb.csv",
        "factorjoin": "results/factorjoin/estimate/imdb.log",
    },
    "aids": {
        "color": "results/color/estimate/aids_merged_maxmix.log",
        "gcare": "results/gcare/estimate/aids_merged_wj.log",
        "gcard": "results/gcard/estimate/aids_merged_k3_d0.log",
        "pathce": "results/pathce/estimate/aids_merged_k3_d0.log",
        "safebound": "results/safebound/estimate/aids_merged.csv",
        "factorjoin": "results/factorjoin/estimate/aids_merged.log",
    },
}


def normalize_query_name(name: str) -> str:
    name = name.strip().replace("\\", "/")
    name = re.sub(r"\.(sql|json|gcare|txt)$", "", name, flags=re.IGNORECASE)
    name = name.split("/")[-1] if name.startswith("q") else name
    return name


def query_topology(name: str) -> str:
    name = name.strip().replace("\\", "/")
    if "/" in name:
        return name.split("/", 1)[0]
    return "Unknown"


def topology_family(name: str) -> str:
    raw = query_topology(name)
    base = raw.split("_", 1)[0].lower()
    if base == "chain":
        return "Path"
    if base == "star":
        return "Star"
    if base == "tree":
        return "Tree"
    if base == "graph":
        return "Graph"
    return "Other"


def parse_estimate_file(path: Path) -> dict:
    ext = path.suffix.lower()
    if ext == ".csv":
        return parse_csv_estimates(path)
    return parse_log_estimates(path)


def parse_csv_estimates(path: Path) -> dict:
    rows = {}
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if not row:
                continue
            q = row.get("query") or row.get("pattern") or row.get("name")
            est = row.get("estimate")
            if not q or not est:
                continue
            try:
                rows[q.strip()] = float(est)
            except ValueError:
                continue
    return rows


def parse_log_estimates(path: Path) -> dict:
    rows = {}
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#") or s.startswith("query,"):
                continue

            # pathce format: "===== q1.json =====" then "698037164,0.0085"
            if s.startswith("====="):
                continue

            m_space = re.match(r"^(\S+)\s+([0-9eE+\-.]+)\s+([0-9eE+\-.]+)$", s)
            if m_space:
                q, est = m_space.group(1), m_space.group(2)
                try:
                    rows[q] = float(est)
                except ValueError:
                    pass
                continue

            m_colon = re.match(r"^(.+?):\s*([0-9eE+\-.]+|None)\s*,\s*([0-9eE+\-.]+)$", s)
            if m_colon:
                q, est = m_colon.group(1), m_colon.group(2)
                if est != "None":
                    try:
                        rows[q] = float(est)
                    except ValueError:
                        pass
                continue

    # PathCE two-line format parser.
    if not rows:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        current_q = None
        for line in lines:
            s = line.strip()
            mq = re.match(r"^===== (.+) =====$", s)
            if mq:
                current_q = mq.group(1)
                continue
            if current_q and "," in s:
                est = s.split(",", 1)[0].strip()
                try:
                    rows[current_q] = float(est)
                except ValueError:
                    pass
                current_q = None
    return rows


def parse_truth_file(path: Path) -> dict:
    if path.is_dir():
        return parse_truth_dir(path)

    text = path.read_text(encoding="utf-8", errors="ignore")
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    if not lines:
        return {}

    # CSV with header (query,truecard/true_card/cardinality...)
    if "," in lines[0] and any(k in lines[0].lower() for k in ["query", "true", "card"]):
        out = {}
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if not row:
                    continue
                q = row.get("query") or row.get("pattern") or row.get("name")
                t = (
                    row.get("truecard")
                    or row.get("true_card")
                    or row.get("cardinality")
                    or row.get("true")
                )
                if not q or not t:
                    continue
                try:
                    out[q.strip()] = float(t)
                except ValueError:
                    continue
        return out

    # Generic line parser: "query value"
    out = {}
    for s in lines:
        s = s.replace(":", " ")
        parts = [p for p in re.split(r"[,\s]+", s) if p]
        if len(parts) < 2:
            continue
        q = parts[0]
        try:
            val = float(parts[1])
            out[q] = val
        except ValueError:
            continue
    return out


def parse_truth_dir(path: Path) -> dict:
    txt_files = sorted(path.rglob("*.txt"))
    out = {}
    if not txt_files:
        return out

    # lsqb/imdb style: q1.txt, q2.txt, ...
    if all(f.parent == path for f in txt_files):
        for f in txt_files:
            name = f.stem
            val = parse_truth_scalar_file(f)
            if val is not None:
                out[name] = val
        return out

    # aids style: topology/uf_Q_*.txt; map by sorted order -> topology/{idx}
    topo_groups = defaultdict(list)
    for f in txt_files:
        rel = f.relative_to(path)
        if len(rel.parts) < 2:
            continue
        topo_groups[rel.parts[0]].append(f)

    for topo, files in topo_groups.items():
        files = sorted(files)
        for idx, f in enumerate(files):
            val = parse_truth_scalar_file(f)
            if val is None:
                continue
            out[f"{topo}/{idx}"] = val
    return out


def parse_truth_scalar_file(path: Path):
    try:
        s = path.read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        return None
    if not s:
        return None
    first = s.splitlines()[0].strip()
    first = first.replace(",", " ").split()[0]
    try:
        return float(first)
    except ValueError:
        return None


def qerror(est: float, truth: float) -> float:
    e = max(float(est), 1.0)
    t = max(float(truth), 1.0)
    return max(e / t, t / e)


def aggregate_metric(errors: list, metric: str) -> float:
    if not errors:
        return float("nan")
    if metric == "mean":
        return float(np.mean(errors))
    if metric == "p90":
        return float(np.percentile(errors, 90))
    return float(median(errors))


def qerror_per_pattern(est_map: dict, truth_map: dict) -> dict:
    out = {}
    for q, est in est_map.items():
        nq = normalize_query_name(q)
        if nq in truth_map:
            out[nq] = qerror(est, truth_map[nq])
    return out


def qerror_topology_means(est_map: dict, truth_map: dict) -> dict:
    topo_to_errs = defaultdict(list)
    for q, est in est_map.items():
        nq = normalize_query_name(q)
        if nq not in truth_map:
            continue
        topo = query_topology(q)
        topo_to_errs[topo].append(qerror(est, truth_map[nq]))

    return {topo: float(np.mean(errs)) for topo, errs in topo_to_errs.items() if errs}


def signed_log10_error(est: float, truth: float) -> float:
    e = max(float(est), 1.0)
    t = max(float(truth), 1.0)
    v = math.log10(e / t)
    return float(np.clip(v, -10.0, 10.0))


def signed_log10_by_topology(est_map: dict, truth_map: dict) -> dict:
    out = defaultdict(list)
    for q, est in est_map.items():
        nq = normalize_query_name(q)
        if nq not in truth_map:
            continue
        fam = topology_family(q)
        if fam == "Other":
            continue
        out[fam].append(signed_log10_error(est, truth_map[nq]))
    return out


def query_sort_key(name: str):
    m = re.match(r"^q(\d+)$", name)
    if m:
        return (0, int(m.group(1)))
    return (1, name)


def plot_pattern_bars(dataset: str, baseline_to_qe: dict, output_path: Path):
    pattern_set = set()
    for qmap in baseline_to_qe.values():
        pattern_set.update(qmap.keys())
    patterns = sorted(pattern_set, key=query_sort_key)
    if not patterns:
        print(f"[WARN] no q-error data for dataset={dataset}, skip plotting")
        return

    x = np.arange(len(patterns))
    width = 0.12
    fig_w = max(12, len(patterns) * 0.45)
    fig, ax = plt.subplots(figsize=(fig_w, 5.2), dpi=180)

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
    ax.legend(ncol=3, fontsize=9, frameon=False)
    ax.set_title(f"{dataset.upper()}: per-pattern Q-Error", fontsize=12)
    plt.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(output_path)
    plt.close(fig)
    print(f"[OK] Saved figure to: {output_path}")


def plot_aids_topology_boxplot(baseline_to_topo_signed: dict, output_path: Path):
    families = ["Path", "Star", "Tree", "Graph"]
    width = 0.12
    group_gap = 0.5

    data = []
    positions = []
    colors = []
    for gi, fam in enumerate(families):
        group_start = gi * (len(BASELINES) * width + group_gap)
        for bi, baseline in enumerate(BASELINES):
            vals = baseline_to_topo_signed.get(baseline, {}).get(fam, [])
            if not vals:
                continue
            data.append(vals)
            positions.append(group_start + bi * width)
            colors.append(BASELINE_COLORS[baseline])

    if not data:
        print("[WARN] no topology-level data for aids, skip plotting")
        return

    fig, ax = plt.subplots(figsize=(11, 5.6), dpi=180)
    bp = ax.boxplot(
        data,
        positions=positions,
        widths=width * 0.8,
        patch_artist=True,
        tick_labels=[""] * len(data),
        showfliers=False,
    )
    for patch, color in zip(bp["boxes"], colors):
        patch.set_facecolor(color)
        patch.set_edgecolor(color)
        patch.set_alpha(0.95)
    for key in ["whiskers", "caps", "medians"]:
        for artist in bp[key]:
            artist.set_color("#222222")
            artist.set_linewidth(1.0)

    centers = [
        gi * (len(BASELINES) * width + group_gap) + (len(BASELINES) - 1) * width / 2
        for gi in range(len(families))
    ]
    ax.set_xticks(centers)
    ax.set_xticklabels(families, fontsize=11)
    for gi in range(1, len(families)):
        x = gi * (len(BASELINES) * width + group_gap) - group_gap / 2
        ax.axvline(x, linestyle="--", linewidth=0.6, color="#666666")

    ax.axhline(0.0, color="#222222", linewidth=0.8)
    ax.set_ylim(-10.2, 10.2)
    ax.set_yticks([-10, -5, 0, 5, 10])
    ax.set_yticklabels(
        [r"$10^{10}$", r"$10^{5}$", r"$10^{0}$", r"$10^{5}$", r"$10^{10}$"],
        fontsize=10,
    )
    ax.set_ylabel(r"underest. $\leftarrow \log_{10}(\hat{c}/c)\rightarrow$ overest.", fontsize=11)
    ax.set_xlabel("Topology", fontsize=11)
    ax.set_title("AIDS: Accuracy by Query Topology", fontsize=12)
    ax.grid(axis="y", linestyle="--", linewidth=0.6, alpha=0.5)

    legend_handles = [
        plt.Line2D([0], [0], color=BASELINE_COLORS[b], lw=6, label=b) for b in BASELINES
    ]
    ax.legend(handles=legend_handles, ncol=3, frameon=False, fontsize=9, loc="upper left")

    plt.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(output_path)
    plt.close(fig)
    print(f"[OK] Saved figure to: {output_path}")


def plot_imdb_boxplot(baseline_to_qe: dict, output_path: Path):
    data = []
    labels = []
    colors = []
    for baseline in BASELINES:
        vals = [v for v in baseline_to_qe.get(baseline, {}).values() if math.isfinite(v)]
        if not vals:
            continue
        data.append(vals)
        labels.append(baseline)
        colors.append(BASELINE_COLORS[baseline])

    if not data:
        print("[WARN] no q-error data for imdb boxplot, skip plotting")
        return

    fig, ax = plt.subplots(figsize=(9.0, 5.2), dpi=180)
    bp = ax.boxplot(
        data,
        tick_labels=labels,
        patch_artist=True,
        showfliers=False,
    )
    for patch, color in zip(bp["boxes"], colors):
        patch.set_facecolor(color)
        patch.set_edgecolor(color)
        patch.set_alpha(0.95)
    for key in ["whiskers", "caps", "medians"]:
        for artist in bp[key]:
            artist.set_color("#222222")
            artist.set_linewidth(1.0)

    means = [float(np.mean(vals)) for vals in data]
    ax.scatter(np.arange(1, len(data) + 1), means, color="#111111", s=18, zorder=3, label="mean")
    for i, mean in enumerate(means, 1):
        ax.text(i, mean * 1.08, f"{mean:.2g}", ha="center", va="bottom", fontsize=8)

    ax.set_yscale("log")
    ax.set_ylabel("Q-Error", fontsize=11)
    ax.set_xlabel("Baseline", fontsize=11)
    ax.set_title("IMDB: Q-Error Distribution", fontsize=12)
    ax.grid(axis="y", linestyle="--", linewidth=0.6, alpha=0.5)
    ax.legend(frameon=False, fontsize=9)
    plt.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(output_path)
    plt.close(fig)
    print(f"[OK] Saved figure to: {output_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Plot 6 baselines q-error bars for lsqb/imdb/aids."
    )
    parser.add_argument(
        "--workspace",
        default=str(Path(__file__).resolve().parents[1]),
        help="experiment workspace path",
    )
    parser.add_argument("--truth-lsqb", required=True, help="truthcard file for lsqb")
    parser.add_argument("--truth-imdb", required=True, help="truthcard file for imdb")
    parser.add_argument("--truth-aids", required=True, help="truthcard file for aids")
    parser.add_argument(
        "--output-dir",
        default="results",
        help="output directory (relative to workspace)",
    )
    args = parser.parse_args()

    ws = Path(args.workspace).resolve()
    truth_paths = {
        "lsqb": Path(args.truth_lsqb).resolve(),
        "imdb": Path(args.truth_imdb).resolve(),
        "aids": Path(args.truth_aids).resolve(),
    }
    truth_maps = {}
    for ds, p in truth_paths.items():
        if not p.exists():
            raise FileNotFoundError(f"truth file not found for {ds}: {p}")
        tm = parse_truth_file(p)
        truth_maps[ds] = {normalize_query_name(k): v for k, v in tm.items()}
        print(f"[INFO] {ds}: loaded {len(truth_maps[ds])} truth entries from {p}")

    # 1) LSQB and IMDB: per-pattern grouped bars.
    for ds in ["lsqb", "imdb"]:
        baseline_to_qe = {}
        for b in BASELINES:
            ep = ws / DEFAULT_ESTIMATE_PATHS[ds][b]
            if not ep.exists():
                print(f"[WARN] missing estimate file, skip: {ep}")
                baseline_to_qe[b] = {}
                continue
            est_map = parse_estimate_file(ep)
            qe_map = qerror_per_pattern(est_map, truth_maps[ds])
            baseline_to_qe[b] = qe_map
            print(f"[INFO] {ds:>4} | {b:>10} | matched patterns = {len(qe_map)}")

        out = (ws / args.output_dir / f"qerror_{ds}_per_pattern.png").resolve()
        plot_pattern_bars(ds, baseline_to_qe, out)

        if ds == "imdb":
            out = (ws / args.output_dir / "qerror_imdb_box.png").resolve()
            plot_imdb_boxplot(baseline_to_qe, out)

    # 2) AIDS: per-topology boxplot with under/over direction.
    aids_topo_signed = {}
    for b in BASELINES:
        ep = ws / DEFAULT_ESTIMATE_PATHS["aids"][b]
        if not ep.exists():
            print(f"[WARN] missing estimate file, skip: {ep}")
            aids_topo_signed[b] = {}
            continue
        est_map = parse_estimate_file(ep)
        topo_signed = signed_log10_by_topology(est_map, truth_maps["aids"])
        aids_topo_signed[b] = topo_signed
        matched = sum(len(v) for v in topo_signed.values())
        over_n = sum(sum(1 for x in vals if x > 0) for vals in topo_signed.values())
        under_n = sum(sum(1 for x in vals if x < 0) for vals in topo_signed.values())
        print(
            f"[INFO] aids | {b:>10} | matched queries = {matched}, over={over_n}, under={under_n}"
        )

    out = (ws / args.output_dir / "qerror_aids_topology_box.png").resolve()
    plot_aids_topology_boxplot(aids_topo_signed, out)


if __name__ == "__main__":
    main()
