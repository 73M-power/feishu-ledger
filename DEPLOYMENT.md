# Feishu Ledger 部署清单

完整说明请阅读 README.md。

## Render

1. 连接 GitHub 仓库。
2. 创建 Docker Web Service，分支选择 main。
3. Health Check Path 设置为 /healthz。
4. 配置 FEISHU_APP_ID、FEISHU_APP_SECRET、FEISHU_VERIFICATION_TOKEN、FEISHU_REPLY_ENABLED、LEDGER_DATA_DIR。
5. 启用同步时配置 FEISHU_DOC_SYNC_ENABLED、FEISHU_WIKI_TOKEN、FEISHU_BITABLE_TABLE_ID。
6. 使用持久化磁盘挂载 /app/data。
7. 部署后检查 /healthz 和 /api/version。
8. 飞书事件回调填写：

~~~text
https://你的域名/api/feishu/ledger/webhook
~~~

修改飞书权限、事件或环境变量后，重新发布飞书应用，并在 Render 执行 Manual Deploy -> Deploy latest commit。

## 本地 Python

~~~powershell
python server.py --host 127.0.0.1 --port 8787
~~~

## Docker Compose

~~~powershell
docker compose up -d --build
docker compose logs -f
~~~

## Cloudflare Tunnel

启动脚本支持 CF_TUNNEL_TOKEN、CLOUDFLARE_TUNNEL_TOKEN 或 TUNNEL_TOKEN。配置后 start.sh 会启动 cloudflared，再启动账本服务。

## GitHub Actions 保活

工作流文件为 .github/workflows/keepalive.yml。在 GitHub 仓库 Variables 中添加：

~~~text
FEISHU_LEDGER_URL=https://你的域名
~~~

工作流会定时访问 /api/ping，只用于保活，不负责接收飞书消息。
