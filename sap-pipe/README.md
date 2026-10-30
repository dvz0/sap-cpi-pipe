# sap-pipe

CLI to sync SAP Integration Suite design-time content with Git and deploy it to the runtime.

Commands: `update artifact`, `update package`, `deploy`, `sync`, `sync apiproxy`, `sync apiproduct`, `snapshot`, `snapshot restore`.

Environment variables use the `SAP_PIPE_` prefix. The default config file is `~/sap-pipe.yaml`.

## Install

```bash
uv sync
uv run sap-pipe --help
```

Needs `diff` on `PATH` (`diffutils`) and `git` when a command creates a commit.

## Auth

Prefer OAuth client credentials. Basic auth still works for Cloud Integration (CSRF token).

```bash
export SAP_PIPE_TMN_HOST=tenant.example.hana.ondemand.com
export SAP_PIPE_OAUTH_HOST=tenant.authentication.example.hana.ondemand.com
export SAP_PIPE_OAUTH_CLIENTID=...
export SAP_PIPE_OAUTH_CLIENTSECRET=...
```

Hosts without `https://`. Use a BTP service key from a Process Integration Runtime instance (plan `api`) with roles `WorkspacePackagesEdit`, `WorkspacePackagesConfigure`, `WorkspaceArtifactsDeploy`, and `MonitoringDataRead`.

## Example

```bash
sap-pipe update artifact \
  --artifact-id DemoFlow \
  --package-id DemoPackage \
  --dir-artifact "$PWD/DemoPackage/DemoFlow"

sap-pipe deploy --artifact-ids DemoFlow
```

## CI image

[`.github/workflows/build-and-publish.yml`](.github/workflows/build-and-publish.yml) is a sample workflow: unit tests, then Docker build and push to GHCR. Jobs are disabled (`if: false`) so nothing runs in this portfolio repo.

| Trigger | Tags |
|---|---|
| Default branch | `ghcr.io/<org>/sap-pipe:<short-sha>`, `:latest`, and the `version` from `pyproject.toml` if that tag is not in the registry yet |
| Git tag | `ghcr.io/<org>/sap-pipe:<tag>` |

Pin consumers to the version tag (today `0.2.0`), not `:latest`. Bump `version` in `pyproject.toml` when you want a new immutable tag.

## Content packages

Design-time packages live in the sibling directory [`sap-pipe-projects`](../sap-pipe-projects). Its sample CI pulls the `sap-pipe` image via `SAP_PIPE_IMAGE` (for example `ghcr.io/<org>/sap-pipe:0.2.0`).

| Branch | Tenant | What runs |
|---|---|---|
| `dev` | Development | Scheduled sync commits here. `deploy_dev` is manual. |
| `qas` | QA | Push runs `deploy_qas` for packages changed in that push. |
| `main` | Production | Same pattern for production. Sync does not commit here. |
