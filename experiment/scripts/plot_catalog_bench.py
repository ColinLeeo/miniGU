#!/usr/bin/env python3
"""
Plot catalog benchmark results from bench_catalog_results.csv.

Usage:
    python3 plot_catalog_bench.py [path/to/bench_catalog_results.csv]

Generates:
    - catalog_thread_scaling.png   : build time vs threads for each SF
    - catalog_peak_memory.png      : peak memory vs SF
    - catalog_stat_size.png        : statistic size (raw vs compressed) vs SF
"""

import sys
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib
import numpy as np

matplotlib.rcParams["font.family"] = "sans-serif"
matplotlib.rcParams["figure.dpi"] = 150


def load_data(csv_path):
    df = pd.read_csv(csv_path)
    df["sf_num"] = df["sf"].str.replace("sf", "").astype(float)
    df = df.sort_values(["sf_num", "threads", "repeat"])
    return df


def plot_thread_scaling(df, output_dir):
    """Build time vs thread count for each SF."""
    sfs = sorted(df["sf_num"].unique())
    n = len(sfs)
    cols = min(3, n)
    rows = (n + cols - 1) // cols
    fig, axes = plt.subplots(rows, cols, figsize=(5 * cols, 4 * rows), squeeze=False)

    for i, sf_num in enumerate(sfs):
        ax = axes[i // cols][i % cols]
        sf_df = df[df["sf_num"] == sf_num]
        sf_name = sf_df["sf"].iloc[0]

        grouped = sf_df.groupby("threads")["wall_time_ms"].agg(["mean", "std"]).reset_index()
        grouped["std"] = grouped["std"].fillna(0)
        y_vals = grouped["mean"] / 1000
        ax.errorbar(
            grouped["threads"],
            y_vals,
            yerr=grouped["std"] / 1000,
            marker="o",
            capsize=3,
            color="steelblue",
        )
        for x, y in zip(grouped["threads"], y_vals):
            ax.annotate(f"{y:.1f}s", (x, y), textcoords="offset points",
                        xytext=(0, 8), fontsize=7, ha="center", color="steelblue")

        ax.set_xlabel("Threads")
        ax.set_ylabel("Build Time (s)")
        ax.set_title(f"SF = {sf_name}")
        ax.legend(fontsize=8)
        ax.grid(True, alpha=0.3)

    for i in range(n, rows * cols):
        axes[i // cols][i % cols].set_visible(False)

    fig.suptitle("Thread Scaling: Build Time vs Thread Count", fontsize=14)
    plt.tight_layout()
    plt.savefig(f"{output_dir}/catalog_thread_scaling.png")
    plt.close()
    print("Saved: catalog_thread_scaling.png")


def plot_peak_memory(df, output_dir):
    """Peak memory vs SF for each thread count."""
    fig, ax = plt.subplots(figsize=(8, 5))

    sf_labels = sorted(df["sf"].unique(), key=lambda s: float(s.replace("sf", "")))
    x_pos = np.arange(len(sf_labels))

    for threads in sorted(df["threads"].unique()):
        sub = df[df["threads"] == threads]
        grouped = sub.groupby("sf")["peak_mem_mb"].agg(["mean", "std"]).reset_index()
        grouped["std"] = grouped["std"].fillna(0)
        grouped["sf"] = pd.Categorical(grouped["sf"], categories=sf_labels, ordered=True)
        grouped = grouped.sort_values("sf")
        y_vals = grouped["mean"].values / 1024
        ax.errorbar(
            x_pos[:len(grouped)],
            y_vals,
            yerr=grouped["std"].values / 1024,
            marker="s",
            label=f"{threads} threads",
            capsize=3,
        )

    ax.set_xticks(x_pos)
    ax.set_xticklabels(sf_labels)
    ax.set_xlabel("Scale Factor")
    ax.set_ylabel("Peak Memory (GB)")
    ax.set_title("Peak Memory vs Scale Factor")
    ax.legend()
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(f"{output_dir}/catalog_peak_memory.png")
    plt.close()
    print("Saved: catalog_peak_memory.png")


def plot_stat_size(df, output_dir):
    """Raw vs compressed statistic size vs SF."""
    fig, ax = plt.subplots(figsize=(8, 5))

    # Sizes are the same across threads/repeats, take first per SF
    sub = df.groupby("sf").first().reset_index()
    sub = sub[sub["stat_size_mb"] > 0]
    sf_labels = sorted(sub["sf"].unique(), key=lambda s: float(s.replace("sf", "")))
    sub["sf"] = pd.Categorical(sub["sf"], categories=sf_labels, ordered=True)
    sub = sub.sort_values("sf")
    x_pos = np.arange(len(sub))

    raw_vals = sub["stat_size_mb"].values
    comp_vals = sub["compressed_size_mb"].values
    ax.plot(x_pos, raw_vals, marker="D", color="steelblue", label="Raw")
    ax.plot(x_pos, comp_vals, marker="o", color="coral", label="Compressed")

    for i, v in enumerate(raw_vals):
        ax.annotate(f"{v:.1f}", (i, v), textcoords="offset points",
                    xytext=(0, 8), fontsize=7, ha="center", color="steelblue")
    for i, v in enumerate(comp_vals):
        ax.annotate(f"{v:.1f}", (i, v), textcoords="offset points",
                    xytext=(0, -12), fontsize=7, ha="center", color="coral")

    # Add compression ratio annotation
    for i, (_, row) in enumerate(sub.iterrows()):
        if row["compressed_size_mb"] > 0:
            ratio = row["stat_size_mb"] / row["compressed_size_mb"]
            ax.annotate(
                f"{ratio:.1f}x",
                (i, row["compressed_size_mb"]),
                textcoords="offset points",
                xytext=(8, -5),
                fontsize=8,
                color="gray",
            )

    ax.set_xticks(x_pos)
    ax.set_xticklabels(sf_labels)
    ax.set_xlabel("Scale Factor")
    ax.set_ylabel("Size (MB)")
    ax.set_title("Catalog Statistic Size: Raw vs Compressed")
    ax.legend()
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(f"{output_dir}/catalog_stat_size.png")
    plt.close()
    print("Saved: catalog_stat_size.png")


def main():
    csv_path = sys.argv[1] if len(sys.argv) > 1 else "experiment/result/build_catalog/bench_catalog_results.csv"
    output_dir = str(csv_path).rsplit("/", 1)[0] if "/" in csv_path else "."

    df = load_data(csv_path)

    print(f"Loaded {len(df)} rows from {csv_path}")
    print(f"Scale factors: {sorted(df['sf'].unique())}")
    print(f"Thread counts: {sorted(df['threads'].unique())}")
    print()

    plot_thread_scaling(df, output_dir)
    plot_peak_memory(df, output_dir)
    plot_stat_size(df, output_dir)

    print("\nAll plots saved.")


if __name__ == "__main__":
    main()
