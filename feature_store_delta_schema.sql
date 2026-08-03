-- Run these once your SQL warehouse / cluster is back online.
-- Replaces: audit.json, claim_dup_store.json, hash_store.json, json_export_table.json

-- 1. audit_log
-- Event types have different fields (FILE_INGESTED has file_hash/sheets,
-- SHEET_PARSED has sheet/sheet_hash/claim_rows/etc). Rather than a wide table
-- with mostly-null columns, common fields are real columns and everything
-- event-specific goes into `details` as a JSON string. Query it with
-- get_json_object(details, '$.file_hash') when you need a specific field.
CREATE TABLE IF NOT EXISTS documentsignalhub.feature_store.audit_log (
  event       STRING,
  event_time  TIMESTAMP,
  filename    STRING,
  details     STRING   -- JSON string: file_hash, sheets, sheet, sheet_hash, claim_rows, sheet_type, total_rows, total_cols, col_renames, etc.
) USING DELTA;

-- 2. claim_dup_store
-- Empty in your sample, but based on how hash_store is shaped and its role
-- (dedup lookups), this is a keyed store: a dedup key mapping to a record.
-- If your actual claim_dup_store.py builds a different key/value shape,
-- tell me and I'll adjust `record` accordingly.
CREATE TABLE IF NOT EXISTS documentsignalhub.feature_store.claim_dup_store (
  dup_key      STRING,
  record       STRING,   -- JSON string, whatever fields claim_dup_store.py currently stores
  last_updated TIMESTAMP
) USING DELTA;

-- 3. hash_store
-- Keyed by file_hash. sheet_hashes is a nested dict (sheet name -> hash),
-- kept as JSON since sheet count/names vary per file.
CREATE TABLE IF NOT EXISTS documentsignalhub.feature_store.hash_store (
  file_hash    STRING,
  filename     STRING,
  first_seen   TIMESTAMP,
  sheet_hashes STRING   -- JSON string: {"Page 1": "hash...", "Page 2": "hash..."}
) USING DELTA;

-- 4. json_export_table
-- The `json` field itself is a large nested blob (exportDate, sheetMeta,
-- records with per-cell edit tracking) -- kept as a string rather than
-- exploded into columns, since its internal shape depends on the sheet type.
DROP TABLE IF EXISTS documentsignalhub.feature_store.json_export_table;

CREATE TABLE documentsignalhub.feature_store.json_export_table (
  dup_key       STRING,   -- filename|sheet|type — the upsert key
  filename      STRING,
  sheet         STRING,
  export_type   STRING,
  export_time   TIMESTAMP,
  record_count  INT,
  record_json   STRING    -- full entry (export JSON blob + cost_metadata), serialized
) USING DELTA;

CREATE TABLE IF NOT EXISTS documentsignalhub.feature_store.llm_cost_log (
  ts               STRING,
  purpose          STRING,
  model            STRING,
  prompt_tokens    INT,
  output_tokens    INT,
  total_tokens     INT,
  cost_usd         DOUBLE,
  log_date         STRING
) USING DELTA;

CREATE TABLE IF NOT EXISTS documentsignalhub.feature_store.adi_cost_log (
  model      STRING,
  pages      INT,
  cost       DOUBLE,
  timestamp  STRING,
  doc_name   STRING
) USING DELTA;

CREATE TABLE IF NOT EXISTS documentsignalhub.feature_store.field_mapping_memory (
  mapping_key               STRING,     -- upsert key: "<schema_name>|<normalized_source_column>"
  schema_name                STRING,
  source_column_raw           STRING,
  source_column_normalized     STRING,
  resolved_field                STRING,
  method                          STRING,   -- 'semantic' | 'llm' | 'user'
  confidence                     DOUBLE,
  hit_count                      INT,
  last_confirmed_at              TIMESTAMP,
  user_corrected                 BOOLEAN
) USING DELTA;
