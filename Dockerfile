FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml README.md ./
COPY vmware_aria/ vmware_aria/

RUN uv pip install --system .

CMD ["vmware-aria", "mcp"]
