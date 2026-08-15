# The container is the real security boundary. The in-process guardrails are a
# convenience layer on top of it -- never the only thing standing between the agent
# and the host.
FROM python:3.12-slim

# The SDK drives the Claude Code CLI, which needs Node.
RUN apt-get update && apt-get install -y --no-install-recommends \
        nodejs npm git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

RUN useradd --create-home --shell /bin/bash agent
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[api]" && pip install --no-cache-dir ".[dev]"

COPY evals ./evals
COPY tests ./tests
COPY .mcp.json CLAUDE.md ./

RUN mkdir -p /app/workspace /app/runs && chown -R agent:agent /app
USER agent

# AGENCY_API_HOST=0.0.0.0 is right here and wrong on a laptop: inside a container,
# loopback makes the API unreachable from outside it, and the published port -- not the
# bind address -- is what decides exposure. The default in config.py stays 127.0.0.1.
ENV AGENCY_PROFILE=sandboxed \
    AGENCY_WORKSPACE_DIR=/app/workspace \
    AGENCY_RUN_DIR=/app/runs \
    AGENCY_LOG_FORMAT=json \
    PYTHONUNBUFFERED=1 \
    AGENCY_API_HOST=0.0.0.0 \
    AGENCY_API_PORT=8000

EXPOSE 8000

ENTRYPOINT ["agency"]
CMD ["--help"]
