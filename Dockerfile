# Container image for mflbot.
#
# The scheduler runs as the default command. It only ever produces
# recommendations; approving them is a separate, deliberate act
# (`docker exec ... bot pending` / `bot approve <id>`).

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first, so code edits do not invalidate the dependency layer.
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir '.[solver]'

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
    MFLBOT_APPROVAL_SECRET=""

HEALTHCHECK --interval=5m --timeout=30s --start-period=1m \
    CMD ["bot", "status"]

ENTRYPOINT ["bot"]
CMD ["run"]
