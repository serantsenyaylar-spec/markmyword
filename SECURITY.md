# Security Policy

## Supported versions

Security fixes are made on the current `main` branch and included in the next
published deployment. Older snapshots and forks are not supported unless their
maintainer has explicitly committed to supporting them.

## Reporting a vulnerability

**Please do not open a public issue for a suspected vulnerability.**

1. Use GitHub's **Report a vulnerability** control on this repository's
   **Security** tab to send a private advisory to the maintainers.
2. Include a clear description, affected component, reproduction steps, and
   potential impact. Do not include student data, production credentials, or
   other sensitive information in the report.
3. If private reporting is unavailable, contact the repository owner through
   their established private channel and ask for a secure disclosure path.

The maintainers aim to acknowledge a report within 7 days, provide a status
update within 14 days, and coordinate a fix before public disclosure. Please
allow reasonable time for investigation and remediation.

## Deployment safeguards

- Keep Streamlit secrets and service credentials out of version control.
- Configure Google OAuth before exposing the teacher portal.
- Leave the development authentication bypass disabled in hosted deployments.
- Use a Supabase service-role key only in server-side Streamlit secrets; never
  expose it to browser code or a client-side application.
