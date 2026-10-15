# 水木社区 RSS

把水木社区（newsmth.net）若干板块的帖子聚合成标准 RSS，发布到 GitHub Pages。

## 在线地址

- 索引页：https://doane2002cn.github.io/newsmth-rss/
- 合并流：https://doane2002cn.github.io/newsmth-rss/all.xml
- 单板块：`https://doane2002cn.github.io/newsmth-rss/feeds/<key>.xml`
- 运行状态：https://doane2002cn.github.io/newsmth-rss/status.json

## 原理

nForum 自带 RSS 输出口，**无需登录即可访问**，因此本项目不需要任何账号或密钥：

```
板块：https://www.newsmth.net/nForum/rss/board-<板块代码>
十大：https://www.newsmth.net/nForum/rss/topten
```

脚本只做四件事：抓取 → GB2312 转 UTF-8 → 净化正文（去掉发信人、发信站、签名、来源等站内头部）
→ 重新生成 UTF-8 的 RSS。`精彩贴图` 没有 RSS，按首页的图片聚合块解析。

## 源清单

共 41 个源，配置在 [sources.json](./sources.json)：

- 水木特快（`NewExpress`）
- 今日十大热门话题（`topten`）
- 近期热帖（`ShiDa`）
- 知性感性讨论区全部 37 个板块
- 精彩贴图（首页图片聚合块）

改源清单只需要编辑 `sources.json`，不用改代码。

## 运行节制

站点对突发请求很敏感，脚本刻意做了限制：

- **串行**请求，每个源之间间隔 1.8 秒，一轮约 2–3 分钟，绝不并发
- 支持 `If-Modified-Since`，上游未变化时返回 304（0 字节），直接跳过
- 尊重上游 `Cache-Control`（板块 300 秒 / 十大 3600 秒），每小时一轮天然满足
- 单源失败按 5s / 10s 退避重试，最多 3 次
- 一旦检测到 403 或封禁页，**立即终止整轮**，不再继续请求

调度为**每 3 小时一次**（`5 */3 * * *`，即 0/3/6/9/12/15/18/21 点第 5 分）。

不设每小时是有原因的：GitHub Actions 对高频定时任务会大量丢弃，实测你账号下"每小时"
的任务实际只跑成每 3–5 小时一次。与其名义上每小时、实际不准，不如明确 3 小时一轮。
另外源清单或脚本有改动时会通过 `push` 立即重跑一次。

## 本地运行

纯标准库，无第三方依赖：

```bash
python scripts/fetch_feeds.py              # 全量
python scripts/fetch_feeds.py --limit 3    # 只跑前 3 个源（调试）
python scripts/fetch_feeds.py --only food,book,wine
```

产物写入 `docs/`：

- `docs/feeds/*.xml`、`docs/all.xml`：RSS 输出
- `docs/boards.json`：全站板块代码 → 中文名（每周重建，卡片标注来源用）
- `data/store.json`：滚动历史（每个源保留最近 60 条）

## 部署

仓库 Settings → Pages → Source 选择 `main` 分支的 `/docs` 目录。

## 说明

本项目仅为第三方聚合，内容版权归水木社区及原作者所有。数据来自站点公开的 RSS 接口，
请勿提高抓取频率。
