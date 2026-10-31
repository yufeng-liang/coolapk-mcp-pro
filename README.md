# 酷安 MCP Server（coolapk-mcp-pro）

纯只读的酷安社区资料检索 MCP Server——让 Claude Desktop / Cursor / dsh 等 AI 助手直接检索酷安帖子、话题与用户资料。

> **声明**：本项目基于 [Lniosy/coolapk-mcp](https://github.com/Lniosy/coolapk-mcp)（经 [zjy2968466837/coolapk-mcp](https://github.com/zjy2968466837/coolapk-mcp) 增强后平铺引入）二次增强，详见 [NOTICE.md](NOTICE.md)。感谢两位上游作者。

登录态复用酷安桌面端、失效检测与重登、风控回落、图片读取。Python 3.10+（实测 3.14）+ FastMCP。

> 仅用于学习与测试，请勿滥用酷安官方接口。本服务不提供任何写操作（点赞 / 回复 / 关注 / 发帖）。

## 特性

- **9 个只读工具**：搜索、帖子详情、话题、用户资料、用户动态、首页动态、图片读取、登录状态、URL 透传
- **风控回落**：帖子详情接口被验证码拦截时自动改从作者动态列表取全文（`fetch_source` 字段标注来源），回复走独立接口
- **请求节制**：全局限速（相邻请求 ≥1.2 s）、GET 按 TTL 缓存、瞬态错误指数退避重试、验证码端点负缓存冷却
- **搜索净化**：严格关键词过滤 + 去重 + 疑似广告标记，滤掉酷安搜索混入的不相关结果
- **登录态增强（0.2.0）**：auto 模式直接复用酷安桌面端登录态，失效时结构化 `auth_expired` 报错，支持 `login --web / --sms / --cookie` 重登

## 安装

```bash
git clone https://github.com/yufeng-liang/coolapk-mcp-pro.git
cd coolapk-mcp-pro
py -m venv .venv
.venv\Scripts\python -m pip install -e .
```

快速验证：

```bash
.venv\Scripts\coolapk search "Magisk 模块"          # CLI 冒烟
.venv\Scripts\python -m coolapk_mcp.server          # 启动 MCP（stdio，Ctrl+C 退出）
```

> **前置**：token V3 签名需要设备指纹 blob（`~/.coolapk-mcp/auth_blob.bin`），仓库不携带该二进制。首次配置建议直接运行 `.venv\Scripts\coolapk login --cookie "<完整Cookie>"`（或 `--sms`），会引导完成设备码与 blob 初始化；仅使用 `login --web` 时如报 blob 缺失，按提示先走一次 `--sms`/`--cookie` 即可。

## 登录（三通道）

MCP 服务启动即自动管理登录态，无需在客户端配置里塞 Cookie：

| 方式 | 命令 | 说明 |
| - | - | - |
| auto 模式（默认） | 无需操作 | 优先读酷安桌面端 `%APPDATA%\com.coolapk.desktop\accounts.json` 中 `lastLoginUid` 对应账户的完整 Cookie（含 SESSID），无效时回落 `~/.coolapk-mcp/config.json` 的原生三键 token；设备码优先复用 accounts.json 的 `deviceCode` |
| 网页登录 | `.venv\Scripts\coolapk login --web` | 拉起登录窗口，人工完成登录（含滑块 / 验证码）后自动提取 Cookie 写入本地配置 |
| 短信登录 | `.venv\Scripts\coolapk login --sms --phone <手机号>` | 按提示两步完成：识别图形验证码 → 提交短信验证码 |
| Cookie 导入 | `.venv\Scripts\coolapk login --cookie "uid=…;username=…;token=…"` | 从浏览器或桌面端日志手动粘贴 Cookie |

查看登录状态：`.venv\Scripts\coolapk login --status`，或让 agent 调用 `coolapk_login_status` 工具。

## 工具一览

| 工具 | 作用 | 关键参数 |
| - | - | - |
| `coolapk_search` | 搜索帖子 / 用户 / 话题 / 应用，严格过滤 + 去重 + 广告标记 | `keyword`、`type=feed\|user\|feedTopic\|app`、`page`、`strict` |
| `coolapk_feed_detail` | 帖子详情 + 回复（风控自动回落） | `feed_id`、`all_replies`（拉全部一级评论）、`comment_keyword`（评论关键词检索）、`author_uid`（加速回落） |
| `coolapk_topic` | 话题详情 / 话题下动态 | `tag`、`with_feeds`、`page` |
| `coolapk_user_profile` | 用户资料 | `uid` |
| `coolapk_user_feeds` | 用户发布的帖子 | `uid`、`page` |
| `coolapk_home` | 首页动态 | `tab=recommend\|hot\|latest`、`page` |
| `coolapk_read_image` | 读取图片消息（带 Referer 下载，解决图片 CDN 直连 567） | 图片 URL |
| `coolapk_login_status` | 查询当前登录态与登录方式 | 无 |
| `coolapk_get_url` | 只读 GET 透传（访问上表未覆盖的酷安只读接口） | 接口路径与参数 |

前 6 个工具基座已实现；`coolapk_read_image` / `coolapk_login_status` / `coolapk_get_url` 为 0.2.0 新增，参数名以集成后代码为准。

所有工具返回 JSON；出错时返回 `{"error": "…", "error_type": "…"}`，`error_type` 取值：`api`（业务错误）、`captcha`（风控验证码）、`auth_expired`（登录失效）、`network` / `transient`（可重试）、`http`、`parse`。agent 应据 `error_type` 决定重试或引导用户，不要盲目重试。

## 配置项（环境变量）

| 变量 | 默认 | 说明 |
| - | - | - |
| `COOLAPK_LOGIN_MODE` | `auto` | 登录态来源：`auto` = 优先桌面端 accounts.json，回落本地配置；`native` = 仅用本地配置。取值以 auth 实现为准 |
| `COOLAPK_MIN_INTERVAL` | `1.2` | 相邻 API 请求最小间隔（秒） |
| `COOLAPK_MAX_RETRIES` | `2` | 瞬态错误（超时 / 5xx / 429）重试次数 |
| `COOLAPK_NO_CACHE` | 未设 | 设为 `1` 禁用 GET 缓存（调试用） |

数据文件（自动生成，勿提交仓库）：`~/.coolapk-mcp/config.json`（设备码与登录态）、`~/.coolapk-mcp/uid_index.json`（feed_id → 作者 uid 索引，供详情回落）。

## 三端接入

命令统一指向本仓 venv：下文以克隆目录 `D:/coolapk-mcp-pro` 为例，替换为你的实际克隆路径。

### dsh

编辑你的 dsh profile 配置目录下的 `cordis.patch.yml`，追加一个 MCP client 插件行，重启 dsh 会话生效：

```yaml
- id: mcp-coolapk
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: coolapk
    transport: stdio
    command: D:/coolapk-mcp-pro/.venv/Scripts/python.exe
    args:
      - -m
      - coolapk_mcp.server
```

写完后可用 `dsh --profile web --dump-config` 核对新行已合入。

### Claude Desktop

编辑 `%APPDATA%\Claude\claude_desktop_config.json`（Windows 路径 JSON 内用 `\\` 转义）：

```json
{
  "mcpServers": {
    "coolapk": {
      "command": "D:\\coolapk-mcp-pro\\.venv\\Scripts\\python.exe",
      "args": ["-m", "coolapk_mcp.server"]
    }
  }
}
```

### Cursor

编辑项目或全局 `.cursor/mcp.json`（正斜杠路径即可）：

```json
{
  "mcpServers": {
    "coolapk": {
      "command": "D:/coolapk-mcp-pro/.venv/Scripts/python.exe",
      "args": ["-m", "coolapk_mcp.server"]
    }
  }
}
```

## 故障排查

### 登录失效（`error_type: auth_expired`）

1. 让 agent 调 `coolapk_login_status` 或运行 `coolapk login --status` 确认失效；
2. 推荐运行 `coolapk login --web` 重新登录（也可 `--sms` / `--cookie`）；
3. stdio 模式下 MCP 服务进程随客户端会话重启，重登后新会话自动带上新登录态。

### 验证码（`error_type: captcha`）

含义：对应接口触发了酷安风控（未登录访问动态详情尤其容易触发，见 酷安 API 社区实测文档（Coolapk-API-Collect） 实测记录）。客户端已自动处理：该端点冷却 600 秒内不再重试；`coolapk_feed_detail` 自动回落到作者动态列表取全文（`fetch_source: author_feed_fallback`）。人工侧能做的：降低调用频率、稍等再试、完成登录（登录态可显著降低未登录风控概率）。

### 图片打不开（HTTP 567）

酷安图片 CDN `image.coolapk.com` 对非官方直连请求返回 HTTP 567（2026-08 实测），把图片 URL 直接丢给浏览器或下载工具大概率拿不到图。请让 agent 使用 `coolapk_read_image` 工具（带 Referer 下载后返回本地文件）读取关键截图。

### 其它

- `parse`：多为风控页返回 HTML，按验证码处理思路等待或降低频率；
- `network` / `transient`：客户端已自动退避重试，连续失败再检查本机网络与代理；
- 搜索结果质量差：保持 `strict` 默认开启；需要原始结果时显式传 `strict=false`。

## 目录结构

```text
（仓库根）
├── pyproject.toml
├── coolapk_mcp/
│   ├── server.py       # MCP 服务与 9 个工具
│   ├── cli.py          # coolapk 命令行（搜索 / 详情 / login 等）
│   ├── client.py       # API 客户端（风控回落、全量评论检索）
│   ├── config.py       # 登录态持久化（~/.coolapk-mcp）
│   ├── throttle.py     # 限速 / 缓存 / 退避重试
│   ├── postfilter.py   # 搜索过滤、去重、评论关键词检索
│   └── auth/           # token v3 签名与设备码
├── NOTICE.md           # 基座来源声明与本仓改动说明
├── CHANGELOG.md        # 版本变更记录
└── LICENSE             # MIT（承自上游）
```

agent 侧使用指南（何时用酷安搜资料、话题映射表、风控礼仪）见 [skills/coolapk-research/SKILL.md](../skills/coolapk-research/SKILL.md)。

## 许可

MIT（见 [LICENSE](LICENSE)），基座来源与改动声明见 [NOTICE.md](NOTICE.md)。
