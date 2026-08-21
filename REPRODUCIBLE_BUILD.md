# Reproducible builds

Use Python 3.12 and uv 0.11.32: `uv sync --locked --all-extras && uv build`. Regenerate `requirements.container.lock` only from the committed uv lock, then use the digest-pinned Dockerfile and full Git SHA build argument. CI records package artifacts, SBOM, locks, provenance, and image ID. Do not publish or promote.
