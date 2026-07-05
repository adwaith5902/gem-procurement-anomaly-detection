-- Find the actual outlier contracts for your top 3 suspects
SELECT 
    contract_number,
    ministry,
    org_name,
    product_name,
    quantity,
    unit_price,
    total_value,
    contract_date,
    contract_status
FROM contracts_analysis
WHERE product_name IN (
    'HIT Mosquito Repellant Spray',
    'Uniball Blue Roller Ball Pen',
    'Unbranded Type -I (Pocket Shape) Envelope'
)
AND unit_price > (
    SELECT AVG(unit_price) * 5
    FROM contracts_analysis ca2
    WHERE ca2.product_name = contracts_analysis.product_name
)
ORDER BY product_name, unit_price DESC;