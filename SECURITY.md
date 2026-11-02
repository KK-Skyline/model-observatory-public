# Security and privacy

Do not attach trace bodies, credentials, auth.json, session tokens or raw logs to reports. Share redacted metadata and reproduction steps. Services bind only to loopback. Metadata includes local paths and stable identifiers: keep runtime directories private.

Capture installation is opt-in and changes a Codex route and user LaunchAgent. Review these changes locally. No installer runs just by downloading the source. The collector never automatically replays a sent request.

Native --self-test does not export screenshots. Its user-selected JSON report may include local paths in errors: keep reports outside the repository and redact before sharing.
