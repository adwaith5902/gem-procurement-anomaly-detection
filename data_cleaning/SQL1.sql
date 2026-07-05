ALTER TABLE contracts ADD COLUMN contract_type TEXT DEFAULT 'goods';
ALTER TABLE contracts ADD COLUMN quantity_clean REAL;
ALTER TABLE contracts ADD COLUMN total_value_clean REAL;
ALTER TABLE contracts ADD COLUMN unit_price_clean REAL;
ALTER TABLE contracts ADD COLUMN contract_year INTEGER;
ALTER TABLE contracts ADD COLUMN contract_month INTEGER;
ALTER TABLE contracts ADD COLUMN contract_hour INTEGER;