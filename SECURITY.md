# Security Policy

## Project status

This repository is a portfolio snapshot and is not an actively operated trading service. No production credentials or live infrastructure identifiers should be present.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting feature from the repository's **Security** tab. Do not include credentials, account data, or exploitable details in a public issue.

## Credential handling

- Keep `REAL_MODE=false` for local evaluation.
- Store API keys and webhook URLs outside Git.
- Revoke a credential immediately if it is ever committed, even if the commit is later removed.
- Treat all Kubernetes Secret manifests in this repository as templates only.

