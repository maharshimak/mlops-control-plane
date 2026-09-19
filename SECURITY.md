# Security

The API registry is process-local and loses state on restart. JSONL state, lineage and canary helpers are separate utilities, not deployment integrations. Artifact URIs are metadata; artifacts are not uploaded or verified. Registered models enter through gates, but library callers receive mutable model objects and are trusted. Thresholds are supplied by callers rather than an independent governance authority. No authentication, durable transactional registry, cloud deployment or automated rollback is implemented.

Use synthetic or explicitly authorized public data. Do not commit API keys, databases, model credentials, patient records or private employer material. Remote model adapters transmit supplied text to the configured endpoint; choose the provider deliberately.

For a suspected vulnerability, use GitHub private vulnerability reporting if enabled. Otherwise contact the maintainer privately through the [portfolio](https://maharshipatel-portfolio.vercel.app/). Do not put secrets or exploit payloads containing private data in a public issue. No response SLA is promised.
