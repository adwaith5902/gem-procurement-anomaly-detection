-- For normal goods: total_value is correct, unit_price = total_value (wrong)
-- Real unit price = total_value / quantity

UPDATE contracts SET
  contract_type     = 'goods',
  total_value_clean = total_value,
  quantity_clean    = CASE
                        WHEN quantity GLOB '[0-9]*' AND CAST(quantity AS REAL) > 0
                        THEN CAST(quantity AS REAL)
                        ELSE NULL
                      END,
  unit_price_clean  = CASE
                        WHEN quantity GLOB '[0-9]*' AND CAST(quantity AS REAL) > 0
                        AND total_value > 0
                        THEN ROUND(total_value / CAST(quantity AS REAL), 4)
                        ELSE NULL
                      END
WHERE contract_type = 'goods'
  AND total_value IS NOT NULL
  AND total_value > 0;

-- Verify (unit_price_clean should now be MUCH smaller than total_value):
SELECT quantity, total_value, unit_price AS old_unit_price, unit_price_clean AS real_unit_price
FROM contracts
WHERE contract_type='goods' AND unit_price_clean IS NOT NULL
LIMIT 10;