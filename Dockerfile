FROM python:3.13-alpine

RUN addgroup -S mimo && adduser -S mimo -G mimo
RUN mkdir -p /data && chown mimo:mimo /data

COPY mimo_code_proxy.py /app/mimo_code_proxy.py

USER mimo
WORKDIR /app

ENV MIMO_HOST=0.0.0.0
ENV MIMO_PORT=8788
ENV MIMO_KEY=
ENV MIMO_CLIENT_FILE=/data/mimo-client

EXPOSE 8788
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8788/v1/health')" || exit 1

ENTRYPOINT ["python3", "mimo_code_proxy.py"]
