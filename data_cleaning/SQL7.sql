DROP VIEW IF EXISTS contracts_clean;
CREATE VIEW contracts_clean AS
SELECT
  id,
  contract_number,
  org_type,
  ministry,
  department,
  org_name,
  office_zone,
  buyer_designation,
  buying_mode,
  contract_date,
  contract_year  AS year,
  contract_month AS month,
  contract_hour  AS hour,
  contract_type,
  total_value_clean       AS total_value,
  quantity_clean          AS quantity,
  unit_price_clean        AS unit_price,
  contract_status,
  product_name,
  brand,
  -- model field: only meaningful for goods contracts
  CASE WHEN contract_type='goods' THEN model ELSE NULL END AS model,
  scraped_at
FROM contracts
WHERE total_value_clean IS NOT NULL
  AND total_value_clean > 0;

-- Check final row count and coverage:
SELECT contract_type, COUNT(*) n,
       ROUND(AVG(total_value),2) avg_val,
       ROUND(AVG(unit_price),4) avg_unit_price
FROM contracts_clean
GROUP BY contract_type;