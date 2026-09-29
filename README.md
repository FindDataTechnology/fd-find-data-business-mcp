# fd-find-data-business-mcp

Commercial, login-gated MCP server that serves FindData's own crawled data to
paying users. Depends on [`fd-open-data-mcp`](https://pypi.org/project/fd-open-data-mcp/)
as a library and reuses its cache, dispatch, and catalog — but exposes **zero
upstream-provider identity** in responses. Every value is branded `finddata`;
no `source_used` / `real_source_used` key ever reaches a client.

## Internal service token (FDBIZ_INTERNAL_TOKEN)

With the JWT gate on, the deployment may set `FDBIZ_INTERNAL_TOKEN` in the
shared `fd-mcp-env` Secret: bearer tokens equal to that value are accepted
as a service identity (`fd-open-data-mcp-federation`) alongside Logto JWTs —
the lane fd-open-data-mcp's transparent registry reads call through (its
`FD_MCP_BUSINESS_TOKEN`). Any other token still goes through JWKS
verification; the value never grants a user identity.

## Scope support (indicator-scope)

Every domain search tool takes an optional `scope` parameter — a named
retrieval scope managed on the fd-open-data-mcp side (`scope_create` etc.,
shared library `fd_open_data_mcp.scoping`). Because each domain tool IS one
source: an unknown scope errors explicitly (`unknown_scope`), a scope whose
`source_dbs` allow-list excludes this tool's source returns an explainable
empty result (`status: out_of_scope` + the scope metadata), and a permitted
scoped call is disclosed (`scope: {name, summary}`) and counted in the
per-(scope, day) hit statistics. Calls without `scope` (and callers without
a default binding) behave exactly as before.

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
| `legal_docs_search` | Fuzzy-search the authoritative-sources corpus (gov rules / MFA treaties / CCDI / rmfyalk / party regulations) with source & doc_type filters. |
| `legal_docs_read` | One legal document by id; honest `content_available: false` when the body was never fetched. |
| `guide_cases_search` | Fuzzy-search SPC guiding cases & notices; filter by doc_type and reference status (有效/不再参照). |
| `guide_cases_read` | One guiding case by source_id: six structured sections + body + reference status. |
| `wenshu_search` | Fuzzy-search China court judgments (title/case no.) with doc_type/province/year filters. Static snapshot — suspended increments, known gaps incl. 2011–2023. |
| `wenshu_read` | One judgment by doc_id: decoded metadata + full text from the object-store block (honest content-missing states). |
| `ia35_company_search` | Firm profiles by name prefix (latest snapshot year) or exact credit code (all yearly batches, latest row wins). |
| `ia35_reg_counts_read` | New-firm registration counts by year/province/industry; spreadsheet-serial years normalized, no zero-filling. |
| `wb_search_indicators` | Fuzzy-search World Bank (WDI) indicators by Chinese/English name or code. |
| `wb_read` | Annual series for a WDI indicator across countries (ISO3 or Chinese name). |
| `gta_search_variables` | Fuzzy-search GTA listed-company panel variables (1,000+ financial-report indicators). |
| `gta_read` | Firm-year values for one GTA panel variable. |
| `city_search_variables` | Fuzzy-search China city-panel variables. |
| `city_read` | City-year values for one city-panel variable (297 cities, 2000–2024). |
| `registry_coverage` | Per-source indicator-registry coverage: registered / verified counts vs in-source anchors, plus the phase-2 and document-domain boundary. |

## Domain federation (yearbook / law / world bank / GTA / city panel)

The ten domain tools read the Windows data machine's PostgreSQL in place,
read-only, via `FDBIZ_DOMAIN_PG_URL` (e.g.
`postgresql://fdbiz_ro:***@100.64.0.5:5432/postgres`; role `fdbiz_ro` has
SELECT-only grants on `yearbook_catalog`, `law_db`, `world_bank`, `gta_panel`,
`china_city_panel`, `wenshu_db` and `ia35`). If that machine is down, the domain
tools return `{"status": "domain_unavailable"}` while all core tools are
unaffected. Optional tuning env: `FDBIZ_DOMAIN_CONNECT_TIMEOUT` (default 5s),
`FDBIZ_DOMAIN_STATEMENT_TIMEOUT_MS` (default 15000), `FDBIZ_DOMAIN_DEBUG`
(include truncated error text in unavailability responses).

### Wenshu judgment bodies (object store)

`wenshu_read` fetches full text from the zstd-compressed month blocks on
RustFS (S3-compatible) via boto3: `FDBIZ_RUSTFS_ENDPOINT` (default
`http://100.64.0.11:9000`), `FDBIZ_RUSTFS_ACCESS_KEY`, `FDBIZ_RUSTFS_SECRET_KEY`,
`FDBIZ_RUSTFS_REGION` (default `us-east-1`), `FDBIZ_RUSTFS_WENSHU_BUCKET`
(default `wenshu`). When the store is unreachable, reads degrade to decoded
metadata with an honest content-missing note. Corpus caliber: static
snapshot (updates land as one-off batch re-imports; automated incremental
crawling remains suspended), with known interior gaps inside the window
(判决年份 2020 verified empty). **Coverage window (live-queried
2026-09-29): 判决年份 2010–2026, ~16.7M rows** (grew from ~10.8M via the
2026-09-29 re-import), ~99.9% with captured bodies (html/text).

### ia35 firm registry (yearly snapshot caliber)

The `ia35` corpus (firms_full, ~288M rows) accumulates one row per firm per
batch year — the source column IS the batch year (1995–2023). Name-prefix
search serves the latest snapshot year only (`FDBIZ_IA35_SNAPSHOT_YEAR`,
default `2023`, backed by a partial index on that literal); credit-code
lookup serves all batches and returns the newest batch row. **Ops note:**
when a new batch year is loaded, rebuild the partial index on the new year
literal and update the env default together. `ia35_reg_counts_read`
normalizes spreadsheet-serial years (epoch 1899-12-30) in the founded-year
column; unconvertible rows are dropped with the count disclosed.

### Indicator identity: the unified indicator registry

The four indicator-search tools (`yearbook_search_indicators`,
`wb_search_indicators`, `gta_search_variables`, `city_search_variables`)
align their result identity with the unified indicator registry
(`fd_open_data.registry_entries` on the same data machine). Every search
row carries, additively and backward-compatibly:

- `native_code` — the registry anchor, identical to the tool's existing
  `code` / `id` key (yearbook ids are cast to text; GTA anchors are the
  variable name and its semantic codes are the UPPERCASED form);
- `semantic_code` — the registry's unified semantic code, or `null` when
  the entry is unverified or unregistered (the registry quality gate:
  unverified entries never surface a semantic code).

The registry lives in its own database, so enrichment is a separate
fail-soft lookup after the main query — never a cross-database JOIN. If
the registry is unreachable, rows are served with `semantic_code: null`,
exactly like every other degradation path; row counts, existing keys, and
the `{results, count, truncated}` envelope are unchanged.

### Coverage boundary (registry_coverage)

`registry_coverage` reports the registration state live from the registry,
unioned with the static boundary declarations (census anchors as of
2026-09-22; natural growth expected — the tool serves live counts):

| Source | Phase | Registered (census) | Verified (census) | In-source anchors |
|---|---|---|---|---|
| `world_bank` | 第一期（已登记） | 1,499 | 1,499 | 1,499 |
| `yearbook_catalog` | 第一期（已登记） | 53,925 | 0（清洗挂账中） | 53,925 |
| `gta_panel` | 第一期（已登记） | 1,468 | 1,466 | 1,468 |
| `china_city_panel` | 第一期（已登记） | 209 | 209 | 209 |
| `fd_open_data`（镜像概念） | 第一期（已登记） | 452 | 246 | 452 |
| `ia35`（工商注册，161G，实体为主） | 已接入（实体域口径） | 0 | — | 未普查 |
| `wenshu_db`（裁判文书，判决年份 2010–2026） | 已接入（文档域口径） | 0 | — | 未普查 |
| `law_db` | 文档域（非指标口径） | 0 | — | 0 |

`law_db` is deliberately outside the indicator caliber: 法条 / 切片 /
指导案例 are document assets, not indicators — 0 registered entries is
the expected state, not a coverage gap. If the registry is unreachable,
`registry_coverage` fails soft: the static declarations above are served
with a `note` and the verified split left null.

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
