# Security

Keep ROI files, real configurations, Earth Engine credentials, proxy subscriptions, private keys and runtime outputs outside Git. Use `config.local.json` or a directory ignored by Git. The included public-tree check catches obvious artifacts and secrets; it complements manual review.

Do not paste secrets or signed download URLs into public issues. Report a potential vulnerability through GitHub private vulnerability reporting when available, or open an issue containing only a minimal, sanitized description. Revoke any accidentally exposed credential before sharing further details.
