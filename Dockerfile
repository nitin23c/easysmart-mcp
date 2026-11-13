# syntax=docker/dockerfile:1
# easysmart-mcp container image: Python 3.14 and the standard library only.
# Nothing is installed from PyPI; the package is pure Python and copied in.
FROM python:3.14-slim-trixie
LABEL org.opencontainers.image.title="easysmart-mcp" \
      org.opencontainers.image.description="Read-only MCP server for TP-Link Easy Smart switches" \
      org.opencontainers.image.source="https://github.com/nitin23c/easysmart-mcp" \
      org.opencontainers.image.licenses="MIT"
COPY src/easysmart_mcp /usr/local/lib/python3.14/site-packages/easysmart_mcp
RUN python -m compileall -q /usr/local/lib/python3.14/site-packages/easysmart_mcp \
 && useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin easysmart
ENV MCP_BIND=0.0.0.0 \
    MCP_PORT=8080 \
    PYTHONUNBUFFERED=1
USER 10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD ["python", "-I", "-m", "easysmart_mcp", "--healthcheck"]
ENTRYPOINT ["python", "-I", "-m", "easysmart_mcp"]
