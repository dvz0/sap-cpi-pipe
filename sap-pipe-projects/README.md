# sap-pipe-projects

Design-time content for SAP Integration Suite. Packages and iFlows sit here unzipped. CI uses the `sap-pipe` image to upload, deploy, and sync against the tenant.

The CLI is built from `../sap-pipe`. Set `SAP_PIPE_IMAGE` to a pinned tag (for example `ghcr.io/<org>/sap-pipe:0.2.0`). Avoid `:latest` on QA and production.

## Layout

```text
packages.json                    # packages managed by sync/deploy
ci/
  deploy-packages.sh             # which packages to upload on deploy/promote
.github/workflows/               # sample Actions (jobs disabled)
<package-id>/
  <package-id>.json              # optional package metadata
  <artifact-id>/
    META-INF/MANIFEST.MF
    metainfo.prop
    src/main/resources/
      parameters.prop            # design-time values from development
      scenarioflows/integrationflow/*.iflw
    parameters/dev.prop          # configured values per tenant
    parameters/qas.prop
    parameters/prod.prop
```

Commit the unzipped iFlow, not the ZIP from the SAP UI.

## Flow

1. Edit the iFlow in the development tenant and Save as version. Drafts are skipped by sync.
2. A schedule on `dev` runs `sync_from_tenant` and commits onto `dev`.
3. `deploy_dev` is manual (Git → development). It does not run on every push.
4. Merge `dev` → `qas`. `deploy_qas` uploads only packages whose files changed in that push.
5. Merge into `main`. `deploy_prod` does the same against production.

Promote keeps the iFlow as stored in Git. Configured values come from `parameters/qas.prop` or `parameters/prod.prop` via the configuration API. The job fails if that file is missing while `parameters.prop` still defines parameters. Sync from development does not overwrite those files, and it never writes to `qas` or `main`.

## Pipeline

Sample workflows live in [`.github/workflows/`](.github/workflows/). Jobs use `if: false`, so they do not run on push or schedule in this portfolio repo.

| Branch | Tenant | What runs |
|---|---|---|
| `dev` | Development | Scheduled `sync_from_tenant`. `deploy_dev` is manual (`workflow_dispatch`). |
| `qas` | QA | Push runs `deploy_qas`. |
| `main` | Production | Push runs `deploy_prod`. |

| Job | When | What |
|---|---|---|
| `shellcheck` / `test_deploy_script` | Push | Lint and unit-test `ci/deploy-packages.sh` |
| `sync_from_tenant` | Schedule on `dev` | Pull every package in `packages.json` and push to `dev` |
| `snapshot_tenant` | Manual (`workflow_dispatch`) | Snapshot editable packages onto `dev` |
| `deploy_dev` | Manual on `dev` | Restore Git → development. Input `deploy_package`: package id, `all-packages`, or empty (no-op). |
| `deploy_qas` | Push to `qas` | Promote changed packages. Input can override. |
| `deploy_prod` | Push to `main` | Same against production. |

[`packages.json`](packages.json) lists the package technical names. Add an entry when you take on another package.

### First run

1. Publish a `sap-pipe` image and set `SAP_PIPE_IMAGE` in the sample workflow to that tag.
2. Create branches `dev` and `qas` (`main` = production).
3. Create GitHub Environments `dev`, `qas`, and `prod` with secrets `CPI_TMN_HOST`, `CPI_OAUTH_HOST`, `CPI_OAUTH_CLIENT_ID`, `CPI_OAUTH_CLIENT_SECRET`.
4. Fill `packages.json`.
5. To go live, restore the intended `on:` triggers in the workflow files and remove each `if: false`.

This folder is meant as a layout sample next to `sap-pipe/`; the Actions jobs above stay disabled until you enable them.
