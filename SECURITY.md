# Security policy

## Supported versions

| Version | Security support |
|---|---|
| `2.0.0-rc2` | Supported for release-candidate security review |
| `2.0.0-rc1` and earlier | Not supported; reproduce against `rc2` |

This release candidate is not approved for production solely because it appears
in this public repository.

## Report a vulnerability privately

Do not open a public Issue, Discussion, or pull request containing an exploit,
credential, sensitive log, or vulnerability detail.

Use GitHub Private Vulnerability Reporting from the repository **Security**
page when that option is available. If it is not available, contact the
repository-owning organization through a private organizational security
channel and ask for a secure reporting method before sending technical detail.
Repository owners should enable GitHub Private Vulnerability Reporting before
public release.

Include:

- affected version or commit;
- affected component and prerequisite configuration;
- minimal, non-destructive reproduction steps;
- impact and expected behavior;
- a redacted proof of concept, if needed;
- a safe way to contact the reporter.

Do not send:

- passwords, API keys, HMAC or webhook secrets, tokens, or 2FA seeds;
- private keys, real wallet credentials, or recovery material;
- production database dumps, backups, environment files, or full logs;
- personal, merchant, trader, TeamLead, or customer data;
- live payment payloads, transaction evidence, or unredacted infrastructure
  details.

Use synthetic identifiers and test-only data. Stop testing if continued
reproduction could alter balances, settlements, Rolling, TeamLead accounting,
webhook delivery, or third-party systems.

## Response expectations

The maintainers aim to acknowledge a complete private report within two
business days. Triage, remediation, disclosure timing, and credit are
coordinated privately according to severity and release status. This is an
initial-response target, not a guaranteed resolution deadline.

The maintainers may ask the reporter to verify a fix against an isolated test
environment. Do not test against production or a third party without explicit
authorization.

## Disclosure

Allow maintainers reasonable time to investigate and publish a coordinated
fix. Do not publish exploit details while users remain exposed. Security fixes
must pass the same PostgreSQL, migration, concurrency, Docker, secret-scan, and
dependency-audit gates as the release candidate.
