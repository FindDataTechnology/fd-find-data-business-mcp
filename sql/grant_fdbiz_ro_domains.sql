-- Grant fdbiz_ro read-only access to the three topic databases newly
-- federated by fd-find-data-business-mcp (world_bank / gta_panel /
-- china_city_panel). Run as a superuser (postgres) on the Windows data
-- machine. Idempotent — safe to re-run.
--
--   PGPASSWORD=<postgres pw, INFRA-NOTES.md> \
--     psql -h 100.64.0.5 -p 5432 -U postgres -d postgres \
--          -f sql/grant_fdbiz_ro_domains.sql
--
-- Verification (as fdbiz_ro, one per database):
--   world_bank:       SELECT count(*) FROM country;      -- expect 265
--   gta_panel:        SELECT count(*) FROM panel_c0;     -- expect 96029
--   china_city_panel: SELECT count(*) FROM panel_raw;    -- expect 7425
--
-- Least privilege: CONNECT + schema USAGE + SELECT only. No CREATE, no
-- TEMP, no writes. DEFAULT PRIVILEGES covers tables created later by the
-- role that runs this script (postgres), so future ingest tables keep
-- working without re-granting.

GRANT CONNECT ON DATABASE world_bank       TO fdbiz_ro;
GRANT CONNECT ON DATABASE gta_panel        TO fdbiz_ro;
GRANT CONNECT ON DATABASE china_city_panel TO fdbiz_ro;

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

\echo 'grants applied: world_bank, gta_panel, china_city_panel -> fdbiz_ro (SELECT only)'
