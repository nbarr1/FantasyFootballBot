# Container image for mflbot.
#
# The dashboard runs as the default command, with the scheduler in the same
# process (one process, one SQLite writer). It only ever produces
# recommendations; approving one is a separate, deliberate act -- in the
# dashboard, or from the CLI (`docker exec ... bot pending`).
#
# Serving on 0.0.0.0 inside the container requires MFLBOT_WEB_PASSWORD; publish
# the port to 127.0.0.1 on the host, or put TLS in front of it.

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first, so code edits do not invalidate the dependency layer.
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir '.[solver,web]'

# State lives on a volume: the database, the response cache, and the approval
# signing key. Without a volume, every restart loses pending recommendations
# and mints a new signing key (invalidating outstanding approvals).
RUN useradd --create-home --uid 10001 mflbot \
 && mkdir -p /data /app/.cache \
 && chown -R mflbot:mflbot /data /app
VOLUME ["/data"]

USER mflbot

# Set MFLBOT_APPROVAL_SECRET explicitly in a container. If it is unset the key
# is generated into the container filesystem and lost on the next rebuild.
ENV MFLBOT_SECRETS_FILE="" \
    MFLBOT_APPROVAL_SECRET="" \
    MFLBOT_WEB_PASSWORD=""

EXPOSE 8765

HEALTHCHECK --interval=5m --timeout=30s --start-period=1m \
    CMD ["bot", "status"]

ENTRYPOINT ["bot"]
CMD ["serve", "--host", "0.0.0.0", "--with-scheduler"]
