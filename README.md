# GeM Procurement Anomaly Detection

Automated collection and forensic analysis of India's Government e-Marketplace (GeM) procurement contracts, built to surface pricing anomalies and vendor-collusion patterns at scale.

## Problem Statement

Public procurement data is published for transparency, but its scale (hundreds of thousands of contracts across thousands of buyers) makes manual review impractical. This project builds an automated pipeline to:
- Collect large-scale contract data directly from the GeM portal
- Systematically flag statistical price outliers, vendor concentration risk, and suspicious bid-timing patterns
- Surface a prioritized list of contracts worth manual/forensic review

## Dataset

- **454,568 contracts** scraped from the GeM portal (Jan 2024 – Jun 2026)
- **₹32,072 Cr** in total contract value
- Spans **104 ministries**, **1,477 departments**, and **3,989 unique buyer organizations**
- Note: `brand` (used as a vendor proxy) is missing in ~43.8% of rows — this reflects how GeM itself records data, not a scraping gap. Vendor-concentration analysis is explicitly scoped to the subset where it's available.

## Methodology

The detection framework combines three independent, complementary signals:

| Layer | Method | What it catches |
|---|---|---|
| **Vendor concentration** | HHI (Herfindahl–Hirschman Index) computed per ministry using brand as a vendor proxy | Ministries/categories dominated by a small number of suppliers |
| **Price benchmarking** | IQR fencing on log-transformed unit price, grouped by product (min. 5 peer contracts) | Contracts priced far above the statistical norm for that product |
| **Bid-timing clustering** | Vectorized detection of near-identical-value contracts from the same buyer within a 30-minute window | Patterns consistent with contract splitting or coordinated bidding |

Each contract receives a composite **anomaly score** (0–2) based on how many layers flag it — contracts flagged by multiple independent methods are the highest-confidence candidates for review.

## Key Results

- **26,988 contracts (5.9%)** flagged as anomaly candidates by at least one method
- **139 contracts** corroborated by *both* the price-outlier and timing-cluster methods — the highest-confidence review list
- **3,145 contracts (0.69%)** flagged as statistical price outliers
- **23,982 contracts (5.3%)** flagged in near-duplicate timing clusters
- Confirmed case: two near-identical ₹5.5L contracts from the same buyer placed **four minutes apart** under a misclassified product category — a pattern consistent with vendor collusion

## Repository Structure

```
gem-repo/
├── src/
│   ├── scraper.py              # GeM portal scraping pipeline (Selenium + OCR CAPTCHA solving)
│   └── anomaly_detection.py    # HHI / price-outlier / timing-cluster forensic framework
├── notebooks/                  # Exploratory analysis notebooks
├── data/                       # Raw/cleaned data (not committed — see .gitignore)
├── outputs/                    # Generated flagged-contract CSVs and summary stats
├── requirements.txt
└── README.md
```

## How to Run

```bash
pip install -r requirements.txt

# 1. Scrape contracts (produces data/raw_contracts.csv)
python src/scraper.py

# 2. Run the anomaly detection framework
python src/anomaly_detection.py data/raw_contracts.csv
```

Outputs are written to `hhi_by_ministry.csv` and `flagged_anomalies.csv`.

## Tech Stack

Python, Pandas, NumPy, Selenium, OCR (CAPTCHA solving), SQL

## Status & Next Steps

This project is actively being extended. Planned improvements:
- Quantity-splitting detection (same buyer/product, multiple contracts just under a procurement threshold within a short window)
- Manual review pass on the 139 double-flagged contracts to establish a verified (vs. candidate) anomaly count
- Investigate whether missing `brand` data correlates with specific contract types, to better scope future vendor-concentration analysis

## Limitations

- Anomaly flags are **candidates for review**, not confirmed instances of fraud or collusion — the framework is a triage tool, not a legal determination.
- Vendor-concentration (HHI) analysis only covers the ~56% of contracts with brand-level data.
