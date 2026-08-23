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
