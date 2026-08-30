# Security Policy

Mark My Words handles student essays, teacher grades, and OAuth identity, so we
take the confidentiality of that data seriously. This policy describes which
versions are supported and how to report a security problem privately.

## Supported Versions

| Version / branch   | Supported          |
| ------------------ | ------------------ |
| `main` (latest)    | :white_check_mark: |
| Tagged releases    | :white_check_mark: |
| Feature/PR branches| :x: (review only) |

Security fixes are released on `main` and backported to the latest tagged
release where practical. Unsupported branches and forks are not monitored.

## What stays out of this repository

The following must **never** be committed, pasted into issues/chat, or included
in a pull request:

- `.streamlit/secrets.toml`, `.env` and `.env.*` files
- Google (AIza*, GOCSPX-), Groq (gsk_*), or Azure Document Intelligence keys
- Google service-account JSON / PEM / P12 files
- Student scans, photos, transcripts, or grading reports

The `.gitignore` encodes these rules, and CI plus periodic scans re-verify that
no credential has slipped into the working tree or history. If you find one,
report it below rather than committing a "cleanup" that keeps the secret in
history.

## Reporting a Vulnerability

Please **do not** open a public issue for a suspected vulnerability.

Instead, report it privately:

1. Open a **Security Advisory** on GitHub
   (**Security → Advisories → New draft security advisory**).
2. Describe the issue, the affected version, and steps to reproduce.
3. Include the impact and any suggested fix if you have one.

You can expect:

- **Acknowledgment** within 3 business days.
- A **triage decision** (accepted / declined / more information needed) within
  7 business days.
- A **fix and advisory** once the issue is validated, coordinated so that
  Streamlit Community Cloud redeploys from the fixed `main` before public
  disclosure.

We will credit reporters who responsibly disclose a validated issue, unless you
ask to remain anonymous. Please give us a reasonable window to patch before
disclosing publicly.

## Scope

In scope: this application, its Azure Document Intelligence Read OCR route, the
Supabase schema/migrations, Google OAuth handling, and the CI/CodeQL workflows.

Out of scope: third-party services (Google, Azure, Supabase, Streamlit), and
issues that require already-held administrative credentials.
