-- Grant fdbiz_ro read-only access to the topic databases federated by
-- fd-find-data-business-mcp (world_bank / gta_panel / china_city_panel /
-- wenshu_db / ia35 / daas). Run as a superuser (postgres) on the Windows data
-- machine. Idempotent — safe to re-run.
--
--   PGPASSWORD=<postgres pw, INFRA-NOTES.md> \
--     psql -h <db-host> -p 5432 -U postgres -d postgres \
--          -f sql/grant_fdbiz_ro_domains.sql
--
-- Verification (as fdbiz_ro, one per database):
--   world_bank:       SELECT count(*) FROM country;      -- expect 265
--   gta_panel:        SELECT count(*) FROM panel_c0;     -- expect 96029
--   china_city_panel: SELECT count(*) FROM panel_raw;    -- expect 7425
--   daas:             SELECT count(*) FROM sources;      -- expect 21 (2026-10-06)
--                     SELECT count(*) FROM daas_functions;   -- expect 57
--                     SELECT count(*) FROM indicator_rules;  -- expect 391
--                     SELECT count(*) FROM rules;            -- expect 1
--
-- Least privilege: CONNECT + schema USAGE + SELECT only. No CREATE, no
-- TEMP, no writes. DEFAULT PRIVILEGES covers tables created later by the
-- role that runs this script (postgres), so future ingest tables keep
-- working without re-granting.
--
-- daas (facet-mcp-foundation-v1 task 4.2): catalog mirror kept in sync by
-- fd-daas-mcp's sync_postgres (full-replace per run, every row stamped
-- synced_at). Grants were applied on the server 2026-10-06 with the
-- bootstrap; this file keeps the replayable record.

GRANT CONNECT ON DATABASE world_bank       TO fdbiz_ro;
GRANT CONNECT ON DATABASE gta_panel        TO fdbiz_ro;
GRANT CONNECT ON DATABASE china_city_panel TO fdbiz_ro;
GRANT CONNECT ON DATABASE wenshu_db        TO fdbiz_ro;
GRANT CONNECT ON DATABASE ia35             TO fdbiz_ro;
GRANT CONNECT ON DATABASE daas             TO fdbiz_ro;

\connect world_bank
GRANT USAGE ON SCHEMA public TO fdbiz_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO fdbiz_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO fdbiz_ro;

\connect gta_panel
GRANT USAGE ON SCHEMA public TO fdbiz_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO fdbiz_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO fdbiz_ro;

\connect china_city_panel
GRANT USAGE ON SCHEMA public TO fdbiz_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO fdbiz_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO fdbiz_ro;

\connect wenshu_db
GRANT USAGE ON SCHEMA public TO fdbiz_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO fdbiz_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO fdbiz_ro;

\connect ia35
GRANT USAGE ON SCHEMA public TO fdbiz_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO fdbiz_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO fdbiz_ro;

\connect daas
GRANT USAGE ON SCHEMA public TO fdbiz_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO fdbiz_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO fdbiz_ro;

\echo 'grants applied: world_bank, gta_panel, china_city_panel, wenshu_db, ia35, daas -> fdbiz_ro (SELECT only)'
