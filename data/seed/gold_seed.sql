-- DuckDB seed for the local, testable stand-in of the Gold schema.
-- Loaded once into data/seed/gold.duckdb (gitignored, rebuilt automatically
-- if missing) by agent/tools/data_query.py.

CREATE TABLE IF NOT EXISTS dim_patients (
    patient_id VARCHAR PRIMARY KEY,
    mrn VARCHAR,
    full_name VARCHAR,
    birth_date VARCHAR,
    gender VARCHAR,
    region VARCHAR
);

INSERT INTO dim_patients VALUES
    ('pt_00001', 'MRN-000123', 'Jane Alvarez', '1968-04-02', 'female', 'midwest'),
    ('pt_00002', 'MRN-000456', 'Robert Chen', '1954-11-19', 'male', 'northeast'),
    ('pt_00003', 'MRN-000789', 'Maria Gonzalez', '1990-07-23', 'female', 'south');

CREATE TABLE IF NOT EXISTS dim_providers (
    provider_id VARCHAR PRIMARY KEY,
    full_name VARCHAR,
    specialty VARCHAR,
    npi VARCHAR,
    home_unit VARCHAR
);

INSERT INTO dim_providers VALUES
    ('prov_042', 'Dr. Alex Nguyen', 'Critical Care', '1234567890', 'ICU'),
    ('prov_017', 'Dr. Jordan Patel', 'Cardiology', '2345678901', 'Cardiology');

CREATE TABLE IF NOT EXISTS fct_encounters (
    encounter_id VARCHAR PRIMARY KEY,
    patient_id VARCHAR,
    encounter_type VARCHAR,
    unit VARCHAR,
    attending_provider_id VARCHAR,
    status VARCHAR
);

INSERT INTO fct_encounters VALUES
    ('enc_10001', 'pt_00001', 'inpatient', 'ICU', 'prov_042', 'in-progress'),
    ('enc_10002', 'pt_00002', 'inpatient', 'Cardiology', 'prov_017', 'in-progress'),
    ('enc_10003', 'pt_00003', 'emergency', 'ED', 'prov_042', 'discharged');

CREATE TABLE IF NOT EXISTS fct_vitals (
    event_id VARCHAR PRIMARY KEY,
    encounter_id VARCHAR,
    patient_id VARCHAR,
    itemid VARCHAR,
    value DOUBLE,
    valueuom VARCHAR,
    event_ts TIMESTAMP
);

INSERT INTO fct_vitals VALUES
    ('evt-0001', 'enc_10001', 'pt_00001', 'heart_rate', 88.0, 'bpm', TIMESTAMP '2026-09-21 14:00:00'),
    ('evt-0002', 'enc_10001', 'pt_00001', 'spo2', 96.0, '%', TIMESTAMP '2026-09-21 14:00:05'),
    ('evt-0003', 'enc_10002', 'pt_00002', 'heart_rate', 145.0, 'bpm', TIMESTAMP '2026-09-21 14:01:00');

CREATE TABLE IF NOT EXISTS gold_live_vitals_by_unit (
    window_start TIMESTAMP,
    window_end TIMESTAMP,
    unit VARCHAR,
    avg_heart_rate DOUBLE,
    out_of_range_count INTEGER,
    reading_count INTEGER
);

INSERT INTO gold_live_vitals_by_unit VALUES
    (TIMESTAMP '2026-09-21 14:00:00', TIMESTAMP '2026-09-21 14:15:00', 'ICU', 88.0, 0, 2),
    (TIMESTAMP '2026-09-21 14:00:00', TIMESTAMP '2026-09-21 14:15:00', 'Cardiology', 145.0, 1, 1);

-- Encounter history (SCD type 2): one row per version of each encounter,
-- valid from valid_from until valid_to (NULL = still current). enc_10001 was
-- admitted to the ED and transferred to the ICU at 10:30; enc_10003 was
-- discharged at 12:00.
CREATE TABLE IF NOT EXISTS fct_encounter_history (
    encounter_id VARCHAR,
    patient_id VARCHAR,
    encounter_type VARCHAR,
    unit VARCHAR,
    attending_provider_id VARCHAR,
    status VARCHAR,
    valid_from TIMESTAMP,
    valid_to TIMESTAMP
);

INSERT INTO fct_encounter_history VALUES
    ('enc_10001', 'pt_00001', 'inpatient', 'ED', 'prov_042', 'in-progress', TIMESTAMP '2026-09-21 08:00:00', TIMESTAMP '2026-09-21 10:30:00'),
    ('enc_10001', 'pt_00001', 'inpatient', 'ICU', 'prov_042', 'in-progress', TIMESTAMP '2026-09-21 10:30:00', NULL),
    ('enc_10002', 'pt_00002', 'inpatient', 'Cardiology', 'prov_017', 'in-progress', TIMESTAMP '2026-09-21 09:00:00', NULL),
    ('enc_10003', 'pt_00003', 'emergency', 'ED', 'prov_042', 'in-progress', TIMESTAMP '2026-09-21 06:00:00', TIMESTAMP '2026-09-21 12:00:00'),
    ('enc_10003', 'pt_00003', 'emergency', 'ED', 'prov_042', 'discharged', TIMESTAMP '2026-09-21 12:00:00', NULL);
