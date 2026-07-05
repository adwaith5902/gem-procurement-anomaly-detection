-- contract_date format is D/M/YYYY HH:MM  (e.g. "1/4/2026 09:30")
UPDATE contracts SET
  contract_year  = CAST(SUBSTR(contract_date, INSTR(contract_date,'/')+INSTR(SUBSTR(contract_date,INSTR(contract_date,'/')+1),'/')+2, 4) AS INTEGER),
  contract_month = CAST(SUBSTR(contract_date, INSTR(contract_date,'/')+1,
                    INSTR(SUBSTR(contract_date, INSTR(contract_date,'/')+1), '/') -1) AS INTEGER),
  contract_hour  = CAST(SUBSTR(contract_date, LENGTH(contract_date)-4, 2) AS INTEGER)
WHERE contract_date != '';

-- Quick verify:
SELECT contract_date, contract_year, contract_month, contract_hour
FROM contracts LIMIT 5;