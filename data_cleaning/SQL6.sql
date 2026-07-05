UPDATE contracts SET org_type = 'Unknown'
WHERE LOWER(TRIM(org_type)) = 'organization';