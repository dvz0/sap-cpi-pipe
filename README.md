# sap-pipe

Sample layout for syncing SAP Integration Suite design-time content with Git and deploying it across tenants (GitHub or GitLab).

| Path | What it is |
|---|---|
| [`sap-pipe/`](sap-pipe/) | Python CLI + Docker image |
| [`sap-pipe-projects/`](sap-pipe-projects/) | Unzipped packages/iFlows and deploy scripts |

Sample CI lives under `.github/workflows/` in each folder (GitHub Actions). Jobs are disabled (`if: false`) so they do not run in this portfolio repo. The same flow maps cleanly to GitLab CI if you prefer that runner.
