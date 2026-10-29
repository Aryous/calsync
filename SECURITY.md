# Security

Report suspected vulnerabilities privately using
[GitHub's vulnerability reporting form](https://github.com/Aryous/calsync/security/advisories/new).
Include the affected revision, a minimal reproduction using synthetic data,
and the expected and observed behavior.

Do not include OAuth tokens, app-specific passwords, calendar contents,
pairing links, personal account identifiers, or unredacted debug logs in a
public issue. Revoke any credential that has been exposed.

Security fixes target the latest `main` branch. Older snapshots do not have
separate maintenance branches.

Keep the web UI on localhost or behind a trusted HTTPS reverse proxy. See
the README's privacy and security section for credential storage and
deployment details.
