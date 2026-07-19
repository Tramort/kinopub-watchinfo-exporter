FROM python:3.12-slim

WORKDIR /app

# Install supercronic (cron for containers).
# Latest releases: https://github.com/aptible/supercronic/releases
ARG TARGETARCH
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && case "$TARGETARCH" in \
      amd64) SUPERCRONIC_SHA1SUM=712d2ece75da6f6e530192a151488578153e4e96 ;; \
      arm64) SUPERCRONIC_SHA1SUM=93323899ddca3f1198f1796a4bf4418ed1e7982e ;; \
      *) echo "unsupported arch: $TARGETARCH" >&2; exit 1 ;; \
    esac \
 && SUPERCRONIC="supercronic-linux-${TARGETARCH}" \
 && SUPERCRONIC_URL="https://github.com/aptible/supercronic/releases/download/v0.2.47/${SUPERCRONIC}" \
 && curl -fsSLO "$SUPERCRONIC_URL" \
 && echo "${SUPERCRONIC_SHA1SUM}  ${SUPERCRONIC}" | sha1sum -c - \
 && chmod +x "$SUPERCRONIC" \
 && mv "$SUPERCRONIC" "/usr/local/bin/${SUPERCRONIC}" \
 && ln -s "/usr/local/bin/${SUPERCRONIC}" /usr/local/bin/supercronic \
 && apt-get purge -y curl \
 && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv \
 && uv sync --frozen --no-dev --no-install-project \
 && pip uninstall -y uv

COPY kinopub-exporter.py traktv-importer.py trakt-sonarr-nextup.py docker-entrypoint.sh ./
RUN chmod +x /app/docker-entrypoint.sh \
 && mkdir -p /app/data

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

ENTRYPOINT ["/app/docker-entrypoint.sh"]
