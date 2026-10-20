FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.10.9 /uv /uvx /bin/

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git diffutils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/sap-pipe
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable
ENV PATH="/opt/sap-pipe/.venv/bin:$PATH"

ENTRYPOINT ["sap-pipe"]
