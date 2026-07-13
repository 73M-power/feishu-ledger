FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
    && curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb \
       -o /tmp/cloudflared.deb \
    && dpkg -i /tmp/cloudflared.deb \
    && rm /tmp/cloudflared.deb \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

ENV TZ=Asia/Shanghai
WORKDIR /app

COPY feishu_ledger.py .
COPY feishu_bitable.py .
COPY server.py .
COPY README.md .
COPY start.sh .
RUN chmod +x start.sh

VOLUME /app/data
EXPOSE 8787
CMD ["./start.sh"]