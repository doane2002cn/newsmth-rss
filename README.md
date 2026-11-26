# 水木社区 RSS 与阅读跳转网关

把水木社区（newsmth.net）若干板块聚合成 RSS，并通过 Hermes、飞书和 Cloudflare Worker
把每日推荐送到 PC 与手机。项目当前采用“RSS 发现 + 用户真实终端阅读”的轻量架构。

## 在线地址

- 索引页：https://doane2002cn.github.io/newsmth-rss/
- 合并流：https://doane2002cn.github.io/newsmth-rss/all.xml
- 单板块：`https://doane2002cn.github.io/newsmth-rss/feeds/<key>.xml`
- 运行状态：https://doane2002cn.github.io/newsmth-rss/status.json
- 阅读网关：https://smth-go.12879737.xyz/r/`<board>`/`<topic>`

## 为什么不再由服务端代读帖子

旧阅读页用服务器上的 Playwright 和静态快照代替用户浏览器，最终同时遇到三个问题：

1. 机房出口 IP 与无头浏览器会触发水木的阅读验证和限流。
2. RSS 只是帖子索引，正文会编辑，图片可能后补，跟帖持续增长，静态快照天然不完整。
3. 服务器没有用户的本地登录态，NewExpress 等需要身份的内容无法稳定读取。

现在的职责边界是：

```text
GitHub Actions -> RSS -> Hermes 筛选 -> 飞书图文卡片
                                      |
                                      v
                              Cloudflare Worker
                                      |
                                      v
                          PC/手机系统浏览器（用户登录态）
                                      |
                                      v
                         水木原帖：正文、原图、分页、跟帖
```

- 服务端只负责发现、筛选、路由和首图缓存。
- 正文与跟帖始终从水木实时读取。
- 用户已有的浏览器登录态负责身份、验证码和私有板块访问。
- `smth-reader` 的 `/a/<token>` 快照页退出主链路，只保留短期回滚价值。

## 目录

```text
scripts/fetch_feeds.py     RSS 抓取与重建
sources.json               订阅源清单
hermes/smth_digest.py      推荐、飞书卡片、首图上传
gateway/                   Cloudflare Worker 薄路由
reader/                    旧阅读快照服务（只作回滚参考）
docs/                      GitHub Pages 产物
data/store.json            滚动历史
```

## RSS 数据源

nForum 自带 RSS 输出口，无需账号或密钥：

```text
板块：https://www.newsmth.net/nForum/rss/board-<板块代码>
十大：https://www.newsmth.net/nForum/rss/topten
```

抓取脚本负责：抓取 → GB2312 转 UTF-8 → 清理站内头部与签名 → 生成 UTF-8 RSS。
`精彩贴图` 没有标准 RSS，按首页图片聚合块解析。

共 41 个源，配置在 [sources.json](./sources.json)：

- 水木特快（`NewExpress`）
- 今日十大热门话题（`topten`）
- 近期热帖（`ShiDa`）
- 知性感性讨论区全部 37 个板块
- 精彩贴图（`pictures`）

站点对突发请求敏感，抓取器保持串行、限速、条件请求和封禁即停。定时频率见
`.github/workflows/`；调整源只改 `sources.json`。

## 阅读网关

`gateway/src/index.js` 是约百行的无状态 Cloudflare Worker：

| 路径 | 行为 |
| --- | --- |
| `/r/<board>/<topic>` | 校验后 `302` 到水木原帖 |
| `/r/<board>/<topic>?p=2` | 兼容旧分页参数，最终发送 `?p=2` |
| `/r/<board>/<topic>?page=2` | 新分页别名，最终规范化为 `?p=2` |
| `/healthz` | 健康检查 |
| `/.well-known/assetlinks.json` | 为阶段二 Android App Link 预留 |

安全边界：

- `board` 只允许 `^[A-Za-z0-9_]{1,64}$`。
- `topic` 只允许 1–12 位数字。
- `p/page` 只允许 1–9999，不接受 0、前导零、重复参数或冲突参数。
- 目标域名固定在 `https://www.newsmth.net`，用户输入不能改变 host。
- 飞书专用跳转参数会被剥离，不会带到水木。
- 跳转和 `assetlinks.json` 都使用 `no-store`，避免边缘缓存拖延路由或证书更新。
- GET 与 HEAD 行为一致；HEAD 不返回响应正文。

本地验证：

```powershell
Set-Location gateway
node --test .\test\worker.test.js
npx wrangler dev
npx wrangler deploy
```

`gateway/wrangler.toml` 使用自定义域名 `smth-go.12879737.xyz`。生产部署只改 Worker，
不触碰 Reader 容器和 `main` 分支。

## Hermes 飞书推送

Hermes 从 RSS 生成稳定链接：

```text
https://smth-go.12879737.xyz/r/<board>/<topic>?lk_jump_to_browser=true&lk_mobile_jump_to_browser=true
```

这是飞书官方要求的“系统浏览器打开”参数，必须保留在卡片原始 URL 上。历史卡片沿用相同的
`/r/<board>/<topic>` 结构，因此后续升级 App Link 不需要重发历史消息。

关键环境变量：

```text
SMTH_GATEWAY_BASE=https://smth-go.12879737.xyz
FEISHU_APP_ID=...          # 仅用于换取 tenant_access_token 和上传图片
FEISHU_APP_SECRET=...
SMTH_CARD_IMAGE_LIMIT=8
SMTH_IMAGE_CACHE_FILE=/opt/data/scripts/state/smth_image_cache.json
```

不需要在水木配置密码、Cookie、Playwright 或远程验证码代理。

### 首图流水线

1. 在清洗 RSS HTML 前提取图片 URL，只接受精确白名单 host。
2. 图片请求带水木 Referer，最多 10MB。
3. 用文件魔数识别 JPEG、PNG、GIF、WebP、TIFF、BMP、ICO。
4. 从图片结构读取真实宽高：GIF 不超过 2000×2000，其他格式不超过 12000×12000。
5. 下载、获取飞书 token、上传共享一个 20 秒绝对截止时间；每次请求只获得剩余时间。
6. 图片重定向只允许留在水木图床白名单，跨域重定向直接拒绝。
7. 上传成功后将 `url_hash -> image_key` 缓存 7 天。
8. 任何无图、超限、超时、跨域或飞书错误都只降级为纯文本卡片，不阻塞推送。

### 生产运行

安装脚本前先备份，运行用户必须与 Hermes 数据目录属主一致：

```powershell
ssh -i 'C:\Users\dduan\.ssh\xhs_oracle' root@92.4.220.16 `
  "docker exec 1Panel-hermes-agent-tBZu cp /opt/data/scripts/smth_digest.py /opt/data/scripts/smth_digest.py.bak"

scp -i 'C:\Users\dduan\.ssh\xhs_oracle' .\hermes\smth_digest.py `
  root@92.4.220.16:/home/1panel/apps/hermes-agent/Hermes-Agent/data/scripts/smth_digest.py

ssh -i 'C:\Users\dduan\.ssh\xhs_oracle' root@92.4.220.16 `
  "docker exec --user 10000:10000 -e HOME=/opt/data -e SMTH_GATEWAY_BASE=https://smth-go.12879737.xyz 1Panel-hermes-agent-tBZu python3 /opt/data/scripts/smth_digest.py"
```

本地检查：

```powershell
python -m pytest .\hermes\test_smth_digest.py -q
python .\hermes\smth_digest.py --dry-run
```

## 验收标准

| 检查项 | 通过标准 |
| --- | --- |
| PC 飞书 | 点击后离开飞书，系统浏览器打开 Worker 并跳转原帖 |
| Android 飞书 | 不被内置 WebView 截留，默认浏览器打开原帖 |
| 完整阅读 | 登录后能看到正文、原图、分页和后续跟帖 |
| 私有板块 | `NewExpress` 等使用手机或 PC 的本地登录态读取 |
| 图文卡片 | 有图帖子显示首图；无图或处理失败仍正常发纯文本卡片 |
| 稳定性 | 连续 24–48 小时无 Playwright、Oracle IP 封禁或空白快照 |

## 回滚

当前主链路已经不再依赖 Reader。需要回滚 Hermes 时：

```powershell
ssh -i 'C:\Users\dduan\.ssh\xhs_oracle' root@92.4.220.16 `
  "docker exec 1Panel-hermes-agent-tBZu cp /opt/data/scripts/smth_digest.py.bak /opt/data/scripts/smth_digest.py"
```

Worker 可用 `npx wrangler deployments list` 查看版本，再用 `npx wrangler rollback <version-id>`
回退。旧 Reader 容器建议在阶段一稳定运行 1–2 天后下线；在此之前只读保留。

## 阶段二：App Link（按需）

只有手机网页排版不满足时才启动，Hermes 推送代码和历史卡片链接保持不变：

- Worker 的 `assetlinks.json` 填入修改版 APK 包名和大写 SHA-256 指纹。
- Android 端由系统 App Link 打开修改版 zSMTH 的独立跳板 Activity。
- 未安装 App 或 App Link 失败时，分流页直接打开水木原站，不能回到 `/r/`，否则会循环。
- APK 使用独立包名和固定签名，保留 MIT 许可；不内置水木密码、Cookie 或 HMAC 对称密钥。
- 自定义 `intent://` 的浏览器回退地址必须是水木原帖，且所有 `board/topic/page` 都要白名单校验。

## 测试

```powershell
Set-Location F:\CodexHub\apps\smth-rss
node --test .\gateway\test\worker.test.js
python -m pytest .\hermes\test_smth_digest.py -q

Set-Location .\reader
$env:PYTHONPATH = (Get-Location).Path
python -m pytest .\tests -q
```

## 说明

本项目仅为第三方聚合。内容版权归水木社区及原作者所有，数据来自站点公开 RSS 接口，
请勿提高抓取频率。主阅读路径禁止重新引入服务端 Playwright 抓取或远程验证码代理。
