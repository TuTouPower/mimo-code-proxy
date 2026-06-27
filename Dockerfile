FROM python:3.13-alpine

RUN addgroup -S mimo && adduser -S mimo -G mimo
RUN mkdir -p /data && chown mimo:mimo /data

COPY src/ /app/src/

USER mimo
WORKDIR /app

ENV MIMO_CONFIG=/app/mimo_config.json
ENV MIMO_LOG_LEVEL=INFO
ENV MIMO_FREE_BASE_URL=https://api.xiaomimimo.com

EXPOSE 8788
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8788/v1/health')" || exit 1

ENTRYPOINT ["python3", "-m", "src.mimo_code_proxy"]
