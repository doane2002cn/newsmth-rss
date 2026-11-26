# 水木私人阅读网关

把水木正文和回复转换成适合飞书内浏览的只读页面。飞书卡片只携带绑定不可变内容快照的
HMAC 签名链接，不包含账号、Cookie 或水木验证码。

## 工作方式

```text
RSS ingestion
    -> immutable snapshot
    -> signed article link
    -> read-only snapshot page

snapshot page -> bounded background queue -> best-effort full text
```

1. Hermes 从 RSS 取得候选帖，按 `(board, topic)` 去重后，用 HMAC 请求签名批量写入
   `POST /api/snapshots`。
2. Reader 把标题、摘要、发布时间和来源地址保存为内容寻址、不可变的快照对象；签名文章
   链接固定指向本轮写入的 `snapshot_id`。
3. 普通阅读只读本地全文缓存或 RSS 快照，不访问水木。即使水木登录失效、弹出验证码或
   限流，阅读页仍然可用。
4. 打开摘要快照后，Reader 会把全文补全任务放入有界后台队列。补全成功只增强后续阅读；
   失败只记录日志，不会反馈到用户阅读链路。
5. 水木登录、验证码和 noVNC 仅保留在独立的管理入口，文章链接不能创建管理会话，也不会
   跳转到验证码页。
6. 普通阅读页不调用大模型，不消耗 token。图片仍由受签名的图片代理按需缓存。

管理页会从浏览器中读取水木会话 Cookie。只有 `main[UTMPUSERID]`、
`main[UTMPKEY]`、`main[UTMPNUM]` 三枚 Cookie 齐全且用户不是 `guest` 时才写入
`data/chromium-cookies.json`，权限设为 `0600`。这一步只读取浏览器状态，不额外请求
水木。网关也会在后台自动检查并保存会话，不依赖管理页是否一直打开；浏览器只剩部分
旧 Cookie 或已被水木回写成 `guest` 时，会在下一次抓取前自动用完整快照补齐。用户正在
noVNC 中登录或完成验证码时，自动恢复会暂停 15 分钟，避免旧会话覆盖刚输入的新会话。

推送端不再通过访问 `/a/` 预热文章，因为阅读页不会触发水木采集。旧手工诊断函数
`prewarm_reader_links()` 仍保留，但 `READER_PREWARM_LIMIT` 默认是 `0`，正常任务不会
访问任何文章链接。全文采集由 Reader 后台队列独立完成，
`SNAPSHOT_REFRESH_COOLDOWN_SECONDS` 默认是 900 秒，防止同一篇文章频繁触发水木风控。

## 本地检查

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
.\.venv\Scripts\python -m pytest
```

## 服务器启动

```bash
install -m 600 .env.example .env
openssl rand -hex 32
# 把输出写入 .env 的 READER_SIGNING_SECRET
docker compose up -d --build
docker compose exec smth-reader python -m app.cli login-url
```

登录入口默认 15 分钟有效且只能使用一次。`data/` 保存不可变 RSS 快照、全文缓存、
图片缓存和 Chromium 会话，必须持久化。

如果部署时设置 `BROWSER_CDP_URL`，阅读网关会复用 Oracle 常驻 `ai-workstation`
的 Chromium 与 noVNC：

```text
BROWSER_CDP_URL=http://ai-workstation:9223
VNC_HTTP_URL=http://ai-workstation:6080
VNC_WEBSOCKET_URL=ws://ai-workstation:6080/websockify
```

阅读网关通过 `cdp-proxy` 连接常驻 Chromium，但不复用桌面上的默认标签页或 Cookie。
它会新建独立浏览器上下文，并维护一个带 `window.name=smth-reader-gateway` 标记的
专用标签页；桌面其他水木页面无法再覆盖阅读网关的水木会话。有效 Cookie 会写入受保护
快照，容器或浏览器重启后自动恢复。远程桌面仍只通过带管理会话 Cookie 的
`/admin/vnc/` 代理访问。如果 `BROWSER_CDP_URL` 留空，容器会回退到内置 Xvfb、
x11vnc 和 websockify。

如果设置 `BROWSER_PROXY_SERVER`，Chromium 会把浏览器请求交给指定 SOCKS 代理。默认
部署可以复用宿主机上的 NAS 反向隧道：

```text
BROWSER_PROXY_SERVER=socks5://172.19.0.1:1080
```

转发桥只在 Docker 网关地址上监听，并把流量送回宿主机的 `127.0.0.1:1080`。需要放行
Reader 所在 Docker 网段访问 `172.19.0.1:1080`，不要把该端口暴露到公网。

Compose 默认把容器接入 `ai-workstation_default` 外部网络，不向宿主机发布 8010。
`READER_BIND_HOST` 设为 `0.0.0.0`，Cloudflare Tunnel 使用稳定的 Docker DNS：

```text
smth-reader.12879737.xyz -> http://smth-reader:8010
```

部署机上的外部网络必须已经存在；如果 Tunnel 所在 Compose 项目使用其他网络名，需要
同步修改 `reader/compose.yml` 和 Tunnel ingress。

## Cloudflare Tunnel

Tunnel ingress 增加，并保留全部现有规则和末尾的 `http_status:404`：

```text
smth-reader.12879737.xyz -> http://smth-reader:8010
```

如果沿用同域 Cookie，建议把管理会话限制在 Cloudflare Access 后，或只通过飞书签发的
一次性登录入口使用。
