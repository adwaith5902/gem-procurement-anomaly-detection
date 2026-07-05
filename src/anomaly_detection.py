"""
GeM Procurement Anomaly Detection Framework
--------------------------------------------
Three independent detection layers, combined into a composite risk score:
  1. HHI vendor concentration   -> flags ministries/categories dominated by few brands
  2. Price benchmarking          -> flags contracts priced far above peer median for same product
  3. Bid-timing clustering       -> flags near-identical contracts from same buyer in a short window

Run: python gem_anomaly_framework.py clean_dataset.csv
"""

import sys
import pandas as pd
import numpy as np


# ---------- 1. HHI VENDOR CONCENTRATION ----------
def compute_hhi(df, group_col, entity_col="brand", value_col="total_value"):
    """
    HHI = sum of squared market shares (x10000), computed per group_col
    (e.g. per ministry) across entity_col (e.g. brand as a vendor proxy).
    Rows with missing entity_col are dropped for this calc only, and reported.
    """
    sub = df.dropna(subset=[entity_col])
    dropped_pct = 1 - len(sub) / len(df)
    grouped = sub.groupby([group_col, entity_col])[value_col].sum().reset_index()
    totals = grouped.groupby(group_col)[value_col].transform("sum")
    grouped["share"] = grouped[value_col] / totals
    hhi = grouped.groupby(group_col)["share"].apply(lambda s: (s ** 2).sum() * 10000)
    hhi = hhi.sort_values(ascending=False).rename("hhi")
    print(f"[HHI] {entity_col} missing in {dropped_pct:.1%} of rows -- excluded from this calc.")
    return hhi


# ---------- 2. PRICE BENCHMARKING ----------
def flag_overpricing(df, product_col="product_name", price_col="unit_price",
                      min_group_size=5, iqr_multiplier=3.0):
    """
    Robust outlier flagging per product: uses IQR fences on log(unit_price)
    instead of raw ratio-to-median, since raw prices contain extreme outliers.
    Only flags within products that have enough peer contracts to benchmark against.
    """
    d = df.copy()
    d["log_price"] = np.log1p(d[price_col].clip(lower=0))
    group_sizes = d.groupby(product_col)[price_col].transform("count")
    q1 = d.groupby(product_col)["log_price"].transform(lambda s: s.quantile(0.25))
    q3 = d.groupby(product_col)["log_price"].transform(lambda s: s.quantile(0.75))
    iqr = q3 - q1
    upper_fence = q3 + iqr_multiplier * iqr
    d["overpriced_flag"] = (group_sizes >= min_group_size) & (d["log_price"] > upper_fence)
    return d


# ---------- 3. BID-TIMING CLUSTERING ----------
def flag_timing_clusters(df, buyer_col="org_name", time_col="contract_date",
                          value_col="total_value", window_minutes=30, value_tolerance=0.02):
    """
    Flags consecutive contracts from the same buyer placed within `window_minutes`
    of each other at near-identical value (within `value_tolerance`).
    Vectorized within each buyer group using shift() instead of a python loop.
    """
    d = df.sort_values([buyer_col, time_col]).copy()
    d["prev_time"] = d.groupby(buyer_col)[time_col].shift(1)
    d["prev_value"] = d.groupby(buyer_col)[value_col].shift(1)

    gap_minutes = (d[time_col] - d["prev_time"]).dt.total_seconds() / 60
    value_diff_pct = (d[value_col] - d["prev_value"]).abs() / d["prev_value"].replace(0, np.nan)

    is_pair = (gap_minutes.between(0, window_minutes)) & (value_diff_pct < value_tolerance)
    d["timing_cluster_flag"] = is_pair.fillna(False)
    # also flag the earlier contract in each pair
    later_idx = d.index[d["timing_cluster_flag"]]
    prior_idx = d.groupby(buyer_col).shift(-1).index  # placeholder, corrected below
    d.loc[d.index.isin(later_idx), "timing_cluster_flag"] = True
    d["timing_cluster_flag_prev"] = d.groupby(buyer_col)["timing_cluster_flag"].shift(-1).fillna(False)
    d["timing_cluster_flag"] = d["timing_cluster_flag"] | d["timing_cluster_flag_prev"]
    return d.drop(columns=["prev_time", "prev_value", "timing_cluster_flag_prev"])


def main(path):
    df = pd.read_csv(path, parse_dates=["contract_date"], dayfirst=True, low_memory=False)
    print(f"Loaded {len(df):,} contracts, {df['total_value'].sum()/1e7:,.0f} Cr total value")

    hhi = compute_hhi(df, group_col="ministry", entity_col="brand")
    hhi.to_csv("hhi_by_ministry.csv")
    print(f"[HHI] Top 5 most concentrated ministries:\n{hhi.head(5)}\n")

    df = flag_overpricing(df)
    print(f"[Price] Flagged {df['overpriced_flag'].sum():,} contracts as statistical price outliers "
          f"({df['overpriced_flag'].mean():.2%} of dataset)\n")

    df = flag_timing_clusters(df)
    print(f"[Timing] Flagged {df['timing_cluster_flag'].sum():,} contracts in near-duplicate timing clusters "
          f"({df['timing_cluster_flag'].mean():.2%} of dataset)\n")

    df["anomaly_score"] = df["overpriced_flag"].astype(int) + df["timing_cluster_flag"].astype(int)
    flagged = df[df["anomaly_score"] > 0].sort_values("anomaly_score", ascending=False)
    flagged.to_csv("flagged_anomalies.csv", index=False)
    print(f"Total unique contracts flagged by at least one method: {len(flagged):,}")
    print(f"Contracts flagged by BOTH methods (highest confidence): {(df['anomaly_score']==2).sum():,}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "clean_dataset.csv")
