# Feishu Ledger

面向家庭、情侣和小团队的飞书聊天式账本。

在飞书机器人中发送“今天买菜 83 AA”或“收入 3000 工资”，服务会解析流水、保存账本、同步飞书多维表格，并提供当月统计、历史总结、月度对比、删除和 CSV 导出。

## 功能

- 聊天式记录支出和收入
- 自动识别类别、付款人、参与人和 AA 分摊
- 删除错误记录
- 当月账本、历史总结、月度对比和 CSV 导出
- 新增流水自动同步到飞书多维表格
- 总结只读取流水，不再写入 sum_ 总结行
- 按多维表格的“类型”和“金额”计算收入、支出和结余
- Feishu webhook 去重和过期事件保护

聊天式实时记账必须使用飞书开放平台的企业自建应用。飞书自定义机器人 Webhook 只能发送消息，不能接收群聊消息。

## 项目结构

~~~text
feishu-ledger/
├── server.py                    # HTTP 服务、飞书回调和 REST API
├── feishu_ledger.py              # 解析、记账、删除、汇总、导出
├── feishu_bitable.py             # 飞书多维表格读写
├── notify.py                     # 自定义机器人定时通知
├── start.sh                      # Docker/Render 启动入口
├── Dockerfile                    # Docker 镜像
├── docker-compose.yml             # 本地容器运行
├── render.yaml                   # Render Blueprint
├── .env.example                  # 环境变量模板
├── DEPLOYMENT.md                 # 快速部署清单
└── tests/                        # 核心逻辑测试
~~~

## 一、飞书开放平台

1. 创建企业自建应用，例如 feishu-ledger。
2. 在应用能力中添加机器人。
3. 开通消息接收、发送应用消息或回复消息等权限。
4. 在事件与回调中订阅消息接收事件，通常为 im.message.receive_v1。
5. 选择将事件发送至开发者服务器。
6. 回调地址填写：

~~~text
https://你的域名/api/feishu/ledger/webhook
~~~

7. 创建并发布应用版本。
8. 确认机器人已加入目标私聊或群聊，当前账号在应用可用范围内。

在“凭证与基础信息”获取：

~~~text
FEISHU_APP_ID
FEISHU_APP_SECRET
~~~

在“事件与回调”的验证配置中获取：

~~~text
FEISHU_VERIFICATION_TOKEN
~~~

不同版本的飞书后台可能使用不同的中文权限名称，搜索“消息与群组”或“接收消息”即可。修改权限或事件订阅后必须重新发布版本。

## 二、飞书多维表格

### 表格字段

建议建立以下字段，字段名需要与代码一致：

~~~text
记录ID
类型
日期
时间
类别
描述
金额
付款人/收款人
参与人
分摊
原始消息
~~~

“类型”字段至少包含两个选项：

~~~text
收入
支出
~~~

### 获取参数

多维表格链接通常类似：

~~~text
https://my.feishu.cn/wiki/页面token?table=tblxxxxxxxx
~~~

配置：

~~~text
FEISHU_WIKI_TOKEN=页面 token
FEISHU_BITABLE_TABLE_ID=tblxxxxxxxx
~~~

FEISHU_WIKI_TOKEN 只填写 /wiki/ 后面的页面 token。FEISHU_BITABLE_TABLE_ID 填写 table= 后的 tbl... 值。

如果能直接获取 Base/App Token，也可以配置：

~~~text
FEISHU_BITABLE_APP_TOKEN=base 或 app token
~~~

### 两层授权

API 权限和表格资源权限都必须配置：

1. 在多维表格分享/文档权限中，把企业自建应用或机器人加入协作者。
2. 至少授予查看、添加和编辑记录的权限。
3. 在开放平台开通多维表格应用身份权限 bitable:app。
4. 修改权限后重新发布应用版本。

收到 code=91403 Forbidden 时，通常是 API 权限开通了，但应用没有这张表的资源权限。

## 三、Render 部署

1. 在 Render 选择 New -> Web Service。
2. 连接 GitHub 仓库：
   https://github.com/73M-power/feishu-ledger
3. 分支选择 main，Runtime 选择 Docker。
4. Health Check Path 设置为 /healthz。
5. 使用仓库内 render.yaml，或手动填写同名配置。
6. 配置环境变量：

~~~text
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
FEISHU_VERIFICATION_TOKEN=xxx
FEISHU_REPLY_ENABLED=1
LEDGER_DATA_DIR=/app/data
FEISHU_DOC_SYNC_ENABLED=1
FEISHU_WIKI_TOKEN=xxx
FEISHU_BITABLE_TABLE_ID=tblxxxxxxxx
~~~

可选变量：

~~~text
FEISHU_BITABLE_APP_TOKEN=xxx
FEISHU_BITABLE_VIEW_ID=vewxxxxxxxx
FEISHU_MAX_CALLBACK_AGE_SECONDS=600
TZ=Asia/Shanghai
~~~

render.yaml 将 /app/data 作为持久化磁盘。生产环境建议使用带持久化磁盘的实例；免费实例可能休眠，容器临时文件系统也可能在重启或重新部署后丢失数据。

部署后检查：

~~~text
https://你的-render-域名.onrender.com/healthz
https://你的-render-域名.onrender.com/api/version
~~~

飞书回调地址：

~~~text
https://你的-render-域名.onrender.com/api/feishu/ledger/webhook
~~~

修改飞书权限、事件或环境变量后，重新发布飞书应用，并在 Render 执行 Manual Deploy -> Deploy latest commit。

## 四、本地运行

要求 Python 3.11 或更高版本，运行时只使用 Python 标准库。

~~~powershell
cd feishu-ledger
python server.py --host 127.0.0.1 --port 8787
~~~

默认数据保存到 data/feishu_ledger.json。PowerShell 配置示例：

~~~powershell
$env:FEISHU_APP_ID = "cli_xxx"
$env:FEISHU_APP_SECRET = "xxx"
$env:FEISHU_VERIFICATION_TOKEN = "xxx"
$env:FEISHU_REPLY_ENABLED = "1"
$env:LEDGER_DATA_DIR = "D:\ledger-data"
python server.py --host 127.0.0.1 --port 8787
~~~

本地服务必须有公网 HTTPS 地址，飞书才能发送事件。可以使用 Cloudflare Tunnel 或 Render。

## 五、Docker Compose

~~~powershell
cd feishu-ledger
docker compose up -d --build
docker compose logs -f
~~~

本地地址为 http://127.0.0.1:8787。

## 六、聊天命令

### 记录

~~~text
今天买菜 83 AA
昨天外卖 56 我付
今天打车 35
收入 3000 工资
入账 2000 奖金
~~~

支出默认按成员 AA，可以调用 /api/ledger/settings 配置成员。

### 查看

~~~text
账本本月       # 当前月份
本月账本       # 当前月份别名
历史总结       # 按月份查看历史
全部账本       # 历史账本别名
账本明细       # 当前月份流水
账本对比       # 最近 6 个月对比
导出本月       # CSV 下载链接
账本帮助
~~~

### 修改和同步

~~~text
删除上一笔
删除今天买菜 83
同步飞书表格
清理总结行
~~~

开启多维表格同步后，新收入和支出会自动写入表格。同步飞书表格只用于补同步历史本地数据，正常使用不需要每次发送。

## 七、汇总口径

连接到飞书多维表格并成功读取时，飞书表格是总结的数据源，不会混入本地旧 JSON。

~~~text
类型=收入的金额相加 = 收入
类型=支出的金额相加 = 支出
结余 = 收入 - 支出
~~~

只有类型严格为“收入”或“支出”的记录参与统计，其他类型会被排除并提示。月份使用“日期”字段，不使用“创建时间”字段。

因此：

- 本月账本不包含上个月日期的记录；
- 类型为收入的记录会按收入计算，即使记录 ID 以 exp_ 开头；
- 支出分类是类别汇总，不是支出笔数；
- 总结不会新增 sum_ 总结记录。

## 八、REST API

~~~text
GET  /healthz
GET  /api/ping
GET  /api/version
GET  /api/ledger?month=2026-07&months=6
GET  /api/ledger/summary?month=2026-07
GET  /api/ledger/comparison?months=6
GET  /api/ledger/export.csv?month=2026-07
POST /api/ledger/settings
POST /api/ledger/entry
POST /api/feishu/ledger/webhook
~~~

手动创建记录：

~~~powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8787/api/ledger/entry -ContentType 'application/json' -Body '{"text":"今天买菜 83 AA","sender":{"name":"Alice"}}'
~~~

配置成员：

~~~json
{
  "members": ["Alice", "Alex"]
}
~~~

## 九、定时推送模式

notify.py 和 monthly-summary.yml 适合使用飞书自定义机器人 Webhook 定时推送总结。

这种模式只能主动发送，不能接收聊天记账。实时记账请使用企业自建应用、server.py 和事件订阅。

~~~text
FEISHU_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx
~~~

## 十、常见问题

### 根路径显示 not found

根路径 / 没有页面是正常的，请访问 /healthz 或 /api/version。

### 机器人找不到或不回复

检查应用是否发布、机器人能力是否开启、账号是否在可用范围内、机器人是否加入目标会话，以及 Render 回调是否可访问。

### 多维表格返回 91403 Forbidden

同时检查 bitable:app API 权限和多维表格分享设置中的应用资源权限。

### 总结没有包含某一行

检查该行的日期和类型。本月账本只统计当前月份；类型不是收入或支出的行不会参与计算。创建时间不会影响月份判断。

### 出现重复回复

检查飞书事件订阅是否重复配置，确认所有事件只发送到一个回调地址。服务会按事件 ID 和消息 ID 去重；临时文件系统可能导致重启后去重状态丢失。

### Render 重新部署后数据消失

确认使用持久化磁盘，并且 LEDGER_DATA_DIR 指向 /app/data。

## 十一、测试

~~~powershell
python -c "from pathlib import Path; files=['feishu_ledger.py','feishu_bitable.py','server.py','notify.py','tests/test_feishu_ledger.py']; [compile(Path(f).read_text(encoding='utf-8'), f, 'exec') for f in files]; print('source compile ok')"
python -m pytest tests/test_feishu_ledger.py -q
git diff --check
~~~

如果没有 pytest：

~~~powershell
python -m pip install pytest
~~~

## 十二、开源与安全

- 项目采用 MIT License，见 LICENSE。
- 安全处理和泄露应对见 SECURITY.md。
- 不要提交 .env、data/、App Secret、Verification Token、Wiki Token 或机器人 Webhook。
- 生产配置放在 Render Environment Variables 或 GitHub Actions Secrets 中。
- 当前账本使用 JSON 文件，适合个人、家庭和小团队；高并发或强审计场景应迁移到数据库。
