# Security Policy

## Reporting a vulnerability

Please **do not open a public issue**. Report privately via GitHub's
[private vulnerability reporting](https://github.com/thecyberthriver/fare-watcher/security/advisories/new)
(Security tab -> Report a vulnerability).

## Secrets

No secrets live in source control. All credentials are supplied at runtime via
environment variables — GitHub Actions Secrets in the cloud, or a git-ignored
`secrets_local.py` locally. If a secret was ever committed, treat it as
compromised and rotate it (BotFather `/revoke` for a Telegram token; the
provider console for an API key).

## Automated checks

- Secret scanning + push protection (public repos)
- Dependabot alerts and security updates
- Least-privilege `GITHUB_TOKEN` (read-only) and SHA-pinned Actions
