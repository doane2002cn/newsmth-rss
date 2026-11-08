# 水木私人阅读网关

把水木正文和回复转换成适合飞书内浏览的只读页面。登录 Cookie 保存在服务端持久化的
Chromium profile 中，飞书卡片只携带 HMAC 签名链接，不包含账号、Cookie 或水木验证码。

## 工作方式

1. 用户通过一次性登录链接进入受保护的远程 Chromium，完成水木登录和 Geetest。
2. 阅读网关在常驻 Chromium 中建立独立浏览器上下文，请求文章接口并清洗正文、回复和图片。
3. 文章缓存 30 分钟，上游不可用时最多回退到 7 天内的快照。
4. 普通阅读页只做 HTML 抓取、解析和缓存，不调用大模型，不消耗 token。

管理页会从浏览器中读取水木会话 Cookie。只有 `main[UTMPUSERID]`、
`main[UTMPKEY]`、`main[UTMPNUM]` 三枚 Cookie 齐全且用户不是 `guest` 时才写入
`data/chromium-cookies.json`，权限设为 `0600`。这一步只读取浏览器状态，不额外请求
水木。网关也会在后台自动检查并保存会话，不依赖管理页是否一直打开；浏览器只剩部分
旧 Cookie 时，会自动用完整快照补齐。

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

登录入口默认 15 分钟有效且只能使用一次。`data/` 保存正文缓存和图片缓存，必须持久化。

默认部署复用 Oracle 常驻 `ai-workstation` 的 Chromium 与 noVNC：

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
