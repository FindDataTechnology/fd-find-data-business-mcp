# fd-find-data-business-mcp

Commercial, login-gated MCP server that serves FindData's own crawled data to
paying users. Depends on [`fd-open-data-mcp`](https://pypi.org/project/fd-open-data-mcp/)
as a library and reuses its cache, dispatch, and catalog — but exposes **zero
upstream-provider identity** in responses. Every value is branded `finddata`;
no `source_used` / `real_source_used` key ever reaches a client.

## Tools

| Tool | Purpose |
|---|---|
| `read` | One indicator for an entity over a list of dates (serves fresh OR stale cache; true miss dispatches in-process). |
| `read_range` | Bulk series for one or more indicators over `[start, end]`. |
| `list_concepts` | Browse all available FindData indicators. |
| `ai_search` | Natural-language indicator + entity discovery (superset of all search tools). |
| `graph_search` | Entity-relationship graph queries (bfs/dfs/neighbors/shortest_path/subgraph/ego_graph/statistics). |
| `yearbook_search_indicators` | Fuzzy-search statistical-yearbook indicators (federated from the Windows data machine). |
| `yearbook_read` | Annual series for a yearbook indicator, optionally one region and year range (latest edition wins). |
| `law_search` | Fuzzy-search Chinese laws/regulations by title with optional category filter. |
| `law_read` | Full text + metadata of one law by id. |
| `wb_search_indicators` | Fuzzy-search World Bank (WDI) indicators by Chinese/English name or code. |
| `wb_read` | Annual series for a WDI indicator across countries (ISO3 or Chinese name). |
| `gta_search_variables` | Fuzzy-search GTA listed-company panel variables (1,000+ financial-report indicators). |
| `gta_read` | Firm-year values for one GTA panel variable. |
| `city_search_variables` | Fuzzy-search China city-panel variables. |
| `city_read` | City-year values for one city-panel variable (297 cities, 2000–2024). |

## Domain federation (yearbook / law / world bank / GTA / city panel)

The ten domain tools read the Windows data machine's PostgreSQL in place,
read-only, via `FDBIZ_DOMAIN_PG_URL` (e.g.
`postgresql://fdbiz_ro:***@100.64.0.5:5432/postgres`; role `fdbiz_ro` has
SELECT-only grants on `yearbook_catalog`, `law_db`, `world_bank`, `gta_panel`
and `china_city_panel`). If that machine is down, the domain tools return
`{"status": "domain_unavailable"}` while all core tools are unaffected.
Optional tuning env: `FDBIZ_DOMAIN_CONNECT_TIMEOUT` (default 5s),
`FDBIZ_DOMAIN_STATEMENT_TIMEOUT_MS` (default 15000), `FDBIZ_DOMAIN_DEBUG`
(include truncated error text in unavailability responses).

## Install

```bash
pip install fd-find-data-business-mcp
```

## Run (local dev, no auth)

```bash
export FD_OPEN_DATA_MCP_DATABASE_URL=postgresql://fd:***@localhost:30432/fd_open_data
fd-find-data-business-mcp serve --transport http --host 0.0.0.0 --port 8310
```

With `FDBIZ_JWKS_URI` unset, auth is disabled (smoke test / local dev). Set it
in deploy to reject every request without a valid Logto-issued JWT (401 before
any tool runs).

## Deploy (zihan k3s, namespace `fd-mcp`)

Auth is Logto (ES384 JWT) via FastMCP's `JWTVerifier`. The canonical Logto
instance runs on guangzhou-xinru:

| Env var | Value |
|---|---|
| `FD_OPEN_DATA_MCP_DATABASE_URL` | `postgresql://fd:***@100.64.0.3:30432/fd_open_data` (mesh address of guangzhou-xinru) |
| `FDBIZ_JWKS_URI` | `https://auth.finddatatech.cloud/oidc/jwks` |
| `FDBIZ_ISSUER` | `https://auth.finddatatech.cloud/oidc` |
| `FDBIZ_AUDIENCE` | the API resource identifier you register in Logto admin (recommended: `https://api.finddatatech.cloud/mcp`) |
| `FDBIZ_ALGORITHM` | JWT signing alg (default `ES384` — Logto's EC P-384 key; set `RS256` if your IdP signs RSA). |

These four keys are added to the shared `fd-mcp-env` Secret (carries
`FD_OPEN_DATA_MCP_DATABASE_URL` / `REDIS_URL` already — the business pod reuses
them; `MCP_BEARER_TOKEN` from the sibling pods is inert here, this pod uses JWT
auth, not bearer):

```bash
# on zihan (sudo kubectl)
sudo kubectl -n fd-mcp create secret generic fd-mcp-env \
  --from-literal=FD_OPEN_DATA_MCP_DATABASE_URL='postgresql://fd:***@100.64.0.3:30432/fd_open_data' \
  --from-literal=REDIS_URL='redis://:***@100.64.0.3:30380/0' \
  --from-literal=FDBIZ_JWKS_URI='https://auth.finddatatech.cloud/oidc/jwks' \
  --from-literal=FDBIZ_ISSUER='https://auth.finddatatech.cloud/oidc' \
  --from-literal=FDBIZ_AUDIENCE='https://api.finddatatech.cloud/mcp' \
  --dry-run=client -o yaml | sudo kubectl apply -f -
sudo kubectl -n fd-mcp apply -f k8s/zihan/40-fd-find-data-business-mcp.yaml
```

Reachable over the mesh at `http://100.64.0.4:30803/mcp` (zihan NodePort 30803).

## Logto: register the API resource

In the Logto admin console (`https://auth-admin.finddatatech.cloud`), create an
**API resource** with identifier `https://api.finddatatech.cloud/mcp` (this
becomes the `aud` claim in issued tokens, and must equal `FDBIZ_AUDIENCE`).
Create a client (M2M or first-party SPA) authorized for that audience; paying
users' tokens must carry `aud: https://api.finddatatech.cloud/mcp`.

## Publish

Token-based publishing — tag `v*` triggers `.github/workflows/release.yml`
(build → twine check → wheel smoke test → publish to PyPI via API token).
Harbor image built by `.github/workflows/docker-publish.yml` on push to
`main` / tags.
