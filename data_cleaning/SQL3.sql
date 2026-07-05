-- For service contracts: quantity field has ₹ symbol
-- model field has the REAL quantity count
-- quantity field has the ₹ total value

UPDATE contracts SET
  contract_type   = 'service',
  -- extract actual quantity from model field (where real qty was stored)
  quantity_clean  = CASE
                      WHEN model GLOB '[0-9]*' THEN CAST(model AS REAL)
                      ELSE NULL
                    END,
  -- extract total value from the ₹quantity field
  total_value_clean = CAST(
                        REPLACE(REPLACE(TRIM(quantity), '₹', ''), ',', '')
                        AS REAL),
  -- unit price = total / qty (if qty > 0)
  unit_price_clean  = CASE
                        WHEN model GLOB '[0-9]*' AND CAST(model AS REAL) > 0
                        THEN CAST(REPLACE(REPLACE(TRIM(quantity),'₹',''),',','') AS REAL)
                             / CAST(model AS REAL)
                        ELSE NULL
                      END
WHERE quantity GLOB '₹*';

-- Verify (should show ~22,642):
SELECT COUNT(*), AVG(quantity_clean), AVG(total_value_clean)
FROM contracts WHERE contract_type = 'service';