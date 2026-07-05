DROP VIEW IF EXISTS contracts_analysis;
-- Step 1: Clean filter for analysis (run this, check row count)
CREATE VIEW contracts_analysis AS
SELECT * FROM contracts_clean
WHERE product_name NOT LIKE '%false%'
  AND product_name NOT LIKE 'Title%'
  AND product_name NOT LIKE 'Item %'
  AND product_name NOT LIKE 'FEPL%'
  AND product_name NOT LIKE 'PBB%'
  AND total_value > 0;

-- Step 2: Price benchmarking — run this, it finds your overpricing cases
SELECT product_name,
       COUNT(*) AS contracts,
       COUNT(DISTINCT ministry) AS ministries,
       MIN(unit_price) AS min_price,
       MAX(unit_price) AS max_price,
       AVG(unit_price) AS avg_price,
       ROUND(MAX(unit_price) / AVG(unit_price), 1) AS max_vs_avg
FROM contracts_analysis
WHERE contract_type = 'goods'
  AND quantity > 1
  AND unit_price BETWEEN 10 AND 500000
GROUP BY product_name
HAVING contracts >= 10
   AND ministries >= 3
ORDER BY max_vs_avg DESC
LIMIT 50;