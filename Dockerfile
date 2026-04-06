FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# Create non-root user
RUN useradd -m -s /bin/bash agent
USER agent
WORKDIR /home/agent

# Install dependencies
COPY --chown=agent pyproject.toml uv.lock* README.md ./
RUN uv sync --locked --no-dev 2>/dev/null || uv sync --no-dev

# Copy source and database
COPY --chown=agent src/ ./src/
COPY --chown=agent data/ ./data/

EXPOSE 9010

ENTRYPOINT ["uv", "run", "src/server.py", "--host", "0.0.0.0", "--port", "9010"]
