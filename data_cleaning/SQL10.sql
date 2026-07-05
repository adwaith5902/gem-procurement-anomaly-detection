-- Your core anomaly detection table
CREATE TABLE IF NOT EXISTS price_anomalies AS
SELECT 
    c.contract_number,
    c.ministry,
    c.org_name,
    c.department,
    c.product_name,
    c.quantity,
    c.unit_price,
    c.total_value,
    c.contract_date,
    c.year,
    c.month,
    c.buying_mode,
    m.median_price,
    m.avg_price,
    m.contract_count,
    ROUND(c.unit_price / m.median_price, 1) AS price_ratio,
    ROUND((c.unit_price - m.median_price) / m.median_price * 100, 1) AS pct_above_median
FROM contracts_analysis c
JOIN (
    SELECT
        product_name,
        AVG(unit_price) AS avg_price,
        COUNT(*) AS contract_count,
        AVG(unit_price) FILTER (
            WHERE rn IN ((cnt + 1) / 2, (cnt + 2) / 2)
        ) AS median_price
    FROM (
        SELECT
            product_name,
            unit_price,
            ROW_NUMBER() OVER (
                PARTITION BY product_name
                ORDER BY unit_price
            ) AS rn,
            COUNT(*) OVER (
                PARTITION BY product_name
            ) AS cnt
        FROM contracts_analysis
        WHERE unit_price > 0
          AND contract_type = 'goods'
          AND quantity > 1
    )
    GROUP BY product_name
    HAVING COUNT(*) >= 10
) m
ON c.product_name = m.product_name
WHERE c.unit_price > m.median_price * 5
  AND c.quantity > 1
ORDER BY price_ratio DESC;

-- Check how many anomalies found
SELECT
    COUNT(*) AS total_anomalies,
    COUNT(DISTINCT ministry) AS ministries_involved,
    COUNT(DISTINCT product_name) AS products_affected,
    SUM(total_value) AS total_value_at_risk,
    SUM(total_value - (median_price * quantity)) AS estimated_overcharge
FROM price_anomalies;