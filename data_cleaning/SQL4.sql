-- For these rows, unit_price IS the total contract value (not per-unit price)
-- So total_value_clean = unit_price, and real unit price = unit_price / quantity

UPDATE contracts SET
  contract_type     = 'goods',
  total_value_clean = unit_price,
  quantity_clean    = CASE
                        WHEN quantity GLOB '[0-9]*' AND CAST(quantity AS REAL) > 0
                        THEN CAST(quantity AS REAL)
                        ELSE NULL
                      END,
  unit_price_clean  = CASE
                        WHEN quantity GLOB '[0-9]*' AND CAST(quantity AS REAL) > 0
                        THEN unit_price / CAST(quantity AS REAL)
                        ELSE NULL
                      END
WHERE contract_type = 'goods'
  AND total_value IS NULL
  AND unit_price IS NOT NULL
  AND quantity GLOB '[0-9]*';

-- Verify:
SELECT COUNT(*), AVG(total_value_clean), AVG(unit_price_clean)
FROM contracts WHERE total_value IS NULL AND unit_price IS NOT NULL AND quantity GLOB '[0-9]*';