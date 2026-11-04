# 酷安 MCP Server（coolapk-mcp-pro）

纯只读的酷安社区资料检索 MCP Server——让任何支持 MCP 的 AI 助手直接检索酷安帖子、话题与用户资料：电脑上的编程工具（dsh / Claude Code / Claude Desktop / Cursor / Cline）、手机上的 AI 助手（RikkaHub / Kelivo / 超级小爱）都可以。

## 接入哪些客户端

**凡是支持 MCP 的客户端都能接入**——客户端只负责发起请求，服务端不用为每个客户端改代码。

| 你的客户端 | 接入指南 |
| - | - |
| 电脑上的编程工具：dsh / Claude Code / Claude Desktop / Cursor / Cline | 见 [AGENT-SETUP.md](AGENT-SETUP.md)，**也可以直接把这个文件丢给你的 AI，让它带你跑通**（该文档就是写给 agent 读的：AI 读完会主动问你是本地还是服务器、用哪种登录，然后一步步装好并验收） |
| 手机上的 AI 助手：RikkaHub / Kelivo / 超级小爱 | 手机端跑不了本地进程，只能连远程形态；图文步骤（含导入 JSON、逐项核对、看图兜底、常见故障）见 [docs/PHONE-RIKKAHUB.md](docs/PHONE-RIKKAHUB.md) |

> 🤖 **如果你是 AI / agent**：先读 [AGENT-SETUP.md](AGENT-SETUP.md)，按里面的三问确认用户场景，再动手安装。

> **声明**：本项目基于 [Lniosy/coolapk-mcp](https://github.com/Lniosy/coolapk-mcp)（经 [zjy2968466837/coolapk-mcp](https://github.com/zjy2968466837/coolapk-mcp) 增强后平铺引入）二次增强，详见 [NOTICE.md](NOTICE.md)。感谢两位上游作者。登录态复用的「酷安桌面端」同样是第三方社区项目，声明见下节。

登录态复用酷安第三方桌面端（见下方准备清单）、失效检测与重登、风控回落、图片读取。Python 3.10+（实测 3.14）+ FastMCP。

> 仅用于学习与测试，请勿滥用酷安官方接口。本服务不提供任何写操作（点赞 / 回复 / 关注 / 发帖）。

## 特性

- **13 个只读工具**：搜索、帖子详情、话题、用户资料、用户动态、首页动态、图片读取、登录状态、搜索联想词、URL 透传、作者画像、作者深挖聚合、图片外链中转
- **风控回落**：帖子详情接口被验证码拦截时自动改从作者动态列表取全文（`fetch_source` 字段标注来源），回复走独立接口
- **请求节制**：全局限速（相邻请求 ≥1.2 s）、GET 按 TTL 缓存、瞬态错误指数退避重试、验证码端点负缓存冷却
- **搜索净化**：严格关键词过滤 + 去重 + 疑似广告标记，滤掉酷安搜索混入的不相关结果
- **登录态增强（0.2.0）**：auto 模式直接复用[酷安第三方桌面端](https://github.com/coolapk-desktop/coolapk-desktop)的登录态（官方没有桌面端，此为社区项目），失效时结构化 `auth_expired` 报错，支持 `login --web / --sms / --cookie / --adb` 重登

## 准备清单（开始前先看这个）

酷安**官方只有手机 App，没有桌面端**。本 MCP 的登录态能力依赖社区维护的第三方桌面客户端，需要自行准备：

| 你的情况 | 需要准备 |
| - | - |
| 路径一（推荐）：安装桌面端 | ① 下载并安装[酷安第三方桌面端](https://github.com/coolapk-desktop/coolapk-desktop/releases)（Windows / macOS / Linux / Android / iOS 均有安装包；原项目 [daimiaopeng/coolapk-desktop](https://github.com/daimiaopeng/coolapk-desktop) 已归档，由社区组织接续维护）→ ② 打开桌面端完成登录（官方授权页扫码或短信）→ ③ 没了，auto 模式自动复用其登录态 |
| 路径二：不装桌面端 | ① 一个酷安**安卓 APK**（任意渠道版本，改名 `coolapk.apk` 放到 `~/.coolapk-mcp/`，用于提取签名 blob）→ ② 用下文短信登录或 Cookie 导入建立登录态 |

> 说明：桌面端登录解决的是**登录态**（accounts.json），签名 blob 仍需安卓 APK（见下节前置）——桌面端的 exe/dmg 安装包提不了 blob。已装桌面端的用户可以顺手把手机上的酷安 APK 也放进来，两步就绪。

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

> **前置**：token V3 签名需要设备指纹 blob（`~/.coolapk-mcp/auth_blob.bin`），仓库不携带该二进制，登录命令本身不会生成 blob。获取方式任选其一：
> 1. 把酷安 APK 改名为 `coolapk.apk` 放到 `~/.coolapk-mcp/`，客户端启动后自动提取 blob 并缓存；
> 2. 从已有可用环境把 `auth_blob.bin` 复制到 `~/.coolapk-mcp/`。

## 登录（四通道）

MCP 服务启动即自动管理登录态，无需在客户端配置里塞 Cookie：

| 方式 | 命令 | 说明 |
| - | - | - |
| auto 模式（默认） | 无需操作 | 优先读酷安第三方桌面端 `%APPDATA%\com.coolapk.desktop\accounts.json` 中 `lastLoginUid` 对应账户的完整 Cookie（含 SESSID），无效时回落 `~/.coolapk-mcp/config.json` 的原生三键 token；设备码优先复用 accounts.json 的 `deviceCode` |
| 网页登录 | `.venv\Scripts\coolapk login --web` | 拉起系统浏览器（Chrome / Edge）打开酷安授权页，人工完成登录（含滑块 / 验证码）后自动经 CDP 读取 Cookie 写入本地配置，300 秒超时。优先走 CDP 直连浏览器（不依赖 pythonnet，**Python 3.14 也可用**），本机找不到浏览器时回退 pywebview 窗口（仅 Windows）；浏览器路径可用 `COOLAPK_BROWSER` 指定，已开调试浏览器可设 `COOLAPK_CDP_PORT` 直连。无图形环境（纯服务器）不可用，此时改用 `--sms` / `--cookie` |
| 短信登录 | `.venv\Scripts\coolapk login --sms --phone <手机号>` | 按提示两步完成：识别图形验证码 → 提交短信验证码 |
| Cookie 导入 | `.venv\Scripts\coolapk login --cookie "uid=…;username=…;token=…"` | 从浏览器或桌面端日志手动粘贴 Cookie |

查看登录状态：`.venv\Scripts\coolapk login --status`，或让 agent 调用 `coolapk_login_status` 工具。

## 工具一览

| 工具 | 作用 | 关键参数 |
| - | - | - |
| `coolapk_search` | 搜索帖子 / 用户 / 话题 / 应用，严格过滤 + 去重 + 广告标记；结果带热度字段 | `keyword`、`type=feed\|user\|feedTopic\|app`、`page`、`strict`、`sort=hot\|reply\|default`（默认 hot，结果质量显著更好） |
| `coolapk_feed_detail` | 帖子详情 + 回复（风控自动回落）；评论聚焦与金矿挖掘 | `feed_id`、`all_replies`、`comment_keyword`、`comments_focus`（`smart`=楼主补充+热门评论，找工具/资源首选；`author`=只看楼主；`hot`=热门评论；`all`=全部）、`with_history`（正文修改历史）、`expand_sub_replies`（深度展开楼中楼，楼主答疑常在其中）；输出含 `share_url`（带分享码的网页链接，人在浏览器打开可看全文，不耗服务器图片流量） |
| `coolapk_topic` | 话题详情 / 话题下动态（默认按热度排序） | `tag`、`with_feeds`、`page`、`hot_first`（默认 true） |
| `coolapk_user_profile` | 用户资料 | `uid` |
| `coolapk_user_feeds` | 用户发布的帖子（支持多页聚合 + 关键词过滤 + 热度排序） | `uid`、`page`、`pages=1-5`、`keyword`、`sort=default\|hot\|latest` |
| `coolapk_home` | 首页动态 | `tab=recommend\|hot\|latest`、`page` |
| `coolapk_read_image` | 读取图片消息（带 Referer 下载，解决图片 CDN 直连 567） | 图片 URL |
| `coolapk_image_link` | 把酷安图片转成可在任意聊天窗口显示的签名 https 链接（仅远程 HTTP 模式，需 `COOLAPK_PUBLIC_BASE`） | `urls`（逗号/空格/换行分隔）、`ttl_minutes`（默认 120） |
| `coolapk_login_status` | 查询当前登录态与登录方式 | 无 |
| `coolapk_suggest` | 搜索联想词（把口语化需求改写成酷安习惯用词） | `keyword` |
| `coolapk_get_url` | 只读 GET 透传（访问上表未覆盖的酷安只读接口） | 接口路径与参数 |
| `coolapk_author` | 作者画像与权威度评分（`author_tier` / `is_expert` / `fans_per_feed` / `expertise`），判断「这人是不是该领域大佬」 | `uid` |
| `coolapk_author_digest` | 作者深挖聚合：一次抓多页他的帖子、按主题过滤并按热度排序，拿到完整攻略链 | `uid`、`keyword`、`pages=1-5`（默认 2）、`top`（默认 10） |

前 6 个工具基座已实现；`coolapk_read_image` / `coolapk_login_status` / `coolapk_get_url` 为 0.2.0 新增；`coolapk_suggest` 为 0.4.0 新增；`coolapk_author` / `coolapk_author_digest` 为 0.5.0 新增；`coolapk_image_link` 为 0.6.0 新增。

帖子类结果统一带质量分：`post_score`（点赞 + 评论×3 + 转发×2）与 `discussion_ratio`（评论/点赞），可直接用于排序和判断「这条是干货还是凑数」。

所有工具返回 JSON；出错时返回 `{"error": "…", "error_type": "…"}`，`error_type` 取值：`api`（业务错误）、`captcha`（风控验证码）、`auth_expired`（登录失效）、`network` / `transient`（可重试）、`http`、`parse`。agent 应据 `error_type` 决定重试或引导用户，不要盲目重试。

## 配置项（环境变量）

| 变量 | 默认 | 说明 |
| - | - | - |
| `COOLAPK_LOGIN_MODE` | `auto` | 登录态来源：`auto` = 优先桌面端 accounts.json，回落本地配置；`desktop` = 锁定仅用桌面端；`native` = 仅用本地配置 |
| `COOLAPK_MIN_INTERVAL` | `1.2` | 相邻 API 请求最小间隔（秒） |
| `COOLAPK_MAX_RETRIES` | `2` | 瞬态错误（超时 / 5xx / 429）重试次数 |
| `COOLAPK_NO_CACHE` | 未设 | 设为 `1` 禁用 GET 缓存（调试用） |
| `COOLAPK_MAX_REPLY_PAGES` | `30` | `all_replies` 全量评论翻页上限（1-100，大帖防刷屏防风控） |
| `COOLAPK_BROWSER` | 未设 | 指定浏览器可执行文件路径（`coolapk login --web` 用）；不设则自动查找 Chrome / Edge |
| `COOLAPK_CDP_PORT` | 未设 | 连接已在运行的浏览器调试端口（需该浏览器带 `--remote-debugging-port` 启动）；设置后 `--web` 不再自己拉起浏览器 |
| `COOLAPK_IMG_THUMBNAIL` | `1` | `/img` 中转缩略优先开关（默认开）：带尺寸后缀的图先取酷安服务端缩略 `.m.jpg`（大图省约 40% 流量），上游非 200 自动回落原图；设 `0` 关闭 |
| `COOLAPK_PUBLIC_BASE` | 未设 | 远程 HTTP 模式下的对外访问前缀（如 `https://mcp.example.com`）。设置后 `coolapk_image_link` 才能生成图片外链，`/img` 中转也随之生效 |

数据文件（自动生成，勿提交仓库）：`~/.coolapk-mcp/config.json`（设备码与登录态）、`~/.coolapk-mcp/uid_index.json`（feed_id → 作者 uid 索引，供详情回落）。

## Agent 接入

**如果你是 AI / agent，请直接读 [AGENT-SETUP.md](AGENT-SETUP.md)**——面向 agent 的完整自助指南：自检环境 → 问清部署形态 → 安装 → 建立登录态 → 写客户端配置 → 五项验收，照做即可完成接入，不需要用户手敲命令。

人读速查（stdio 本地形态，路径换成实际克隆目录）：

**Claude Desktop**（`%APPDATA%\Claude\claude_desktop_config.json`，Windows 路径 JSON 内用 `\\` 转义）：

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

**Cursor**

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

**凡是支持 MCP 的客户端都能接入**，包括手机上的 AI 助手（超级小爱、RikkaHub、Kelivo 等）——手机端通常只能连接远程形态，需要先按[远程部署](#远程部署)把服务跑起来，具体步骤（含导入 JSON、截图与逐项核对）见[手机端接入指南（以 RikkaHub 为例）](docs/PHONE-RIKKAHUB.md)；远程客户端写法见「远程部署 → 客户端接入」，其余桌面客户端按 stdio 三要素（`command` 指向 venv 的 python、`args` 为 `["-m", "coolapk_mcp.server"]`）自行适配，dsh 的 `cordis.patch.yml` 片段见 AGENT-SETUP.md。

## 远程部署

前面的「Agent 接入」示例都是 stdio 形态：MCP 进程跑在本机、随客户端会话启动退出。如果你有一台云服务器，想让家里和公司的电脑、多台设备甚至多个同事共用同一个部署（登录态与缓存也只有一份），就把服务跑成远程形态：服务端以 streamable-http 启动，客户端通过 URL + Bearer Token 远程连接。

### 快速启动

本节命令为 **Linux（bash）服务器** 写法（远程部署的目标环境即 Linux 服务器）；Windows 的本地安装与 stdio 接入见[安装](#安装)与「Agent 接入」两节。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .

# 生成一个随机 token（服务端与客户端共用）
python3 -c "import secrets; print(secrets.token_urlsafe(32))"

# 四个环境变量，然后启动
export COOLAPK_MCP_TRANSPORT=streamable-http   # 启用远程形态（http / streamable_http 别名等效）
export COOLAPK_HTTP_HOST=127.0.0.1             # 一般绑回环，由反代对外；确要直连公网见下方安全一节
export COOLAPK_HTTP_PORT=8000
export COOLAPK_HTTP_TOKEN=<上一步生成的随机串>

.venv/bin/coolapk-mcp
```

启动后先自检：`curl http://127.0.0.1:8000/health` 返回 `{"status":"ok"}` 即说明服务已就绪（该端点免鉴权，仅供探活）；MCP 端点在 `/mcp`，一律要求 Bearer Token。

### 环境变量（远程形态）

| 变量 | 默认 | 说明 |
| - | - | - |
| `COOLAPK_MCP_TRANSPORT` | `stdio` | 传输形态：`streamable-http`（别名 `http` / `streamable_http` 等效）启用远程 HTTP 服务；不设或设 `stdio` 即本地形态 |
| `COOLAPK_HTTP_HOST` | `127.0.0.1` | HTTP 监听地址。绑 `0.0.0.0` 等非回环地址时**必须**同时设置 `COOLAPK_HTTP_TOKEN`，否则服务拒绝启动 |
| `COOLAPK_HTTP_PORT` | `8000` | HTTP 监听端口 |
| `COOLAPK_HTTP_TOKEN` | 空 | Bearer Token。客户端请求须带 `Authorization: Bearer <token>`；校验失败返回 401 |

> **uvicorn workers 必须为 1**：服务内部以单进程方式启动 uvicorn（`uvicorn.Config(...)` 未设 `workers` 参数）。请勿自行用 `--workers N` 多进程拉起——全局限速（`COOLAPK_MIN_INTERVAL`）、GET 缓存与验证码负缓存都是**进程内状态**，多进程互不共享：限速被 worker 数稀释、缓存命中率下降、验证码冷却互不感知，风控风险成倍上升。需要扩容时按「一台服务器一个实例」拆分部署，不要在同一实例上开多进程。

### 安全

- **强制 Bearer Token**：绑非回环地址而未设 `COOLAPK_HTTP_TOKEN` 时服务直接拒绝启动；token 用随机长串（生成命令见快速启动），不要用可猜的短串。
- **必须走 TLS**：Bearer Token 是明文凭据，裸 HTTP 公网传输会被中间人截获。请勿把 `8000` 端口直接暴露公网，务必套一层 TLS 反向代理（Nginx 或 Caddy 示例如下），并在防火墙 / 云安全组里关闭直连端口。
- **Host 校验（DNS rebinding 防护）已关闭**：FastMCP 自带的 DNS rebinding 防护按 Host 头白名单拦截请求，远程部署常态的「公网 IP 直连」会被其误判 421 拒绝，因此本服务显式关闭了该层（`enable_dns_rebinding_protection=False`）。此取舍以 Bearer Token 鉴权强制生效为前提——公网部署务必配置 `COOLAPK_HTTP_TOKEN` 并走 TLS 反代。
- Nginx 反代示例（假设已有 `your-domain` 的证书配置）：

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;   # MCP 流式响应需要关闭缓冲
    proxy_read_timeout 300s;
}
```

- Caddy 反代示例（自动申请证书，两行即可）：

```caddyfile
your-domain {
    reverse_proxy 127.0.0.1:8000
}
```

### systemd 常驻

```ini
# /etc/systemd/system/coolapk-mcp.service
[Unit]
Description=Coolapk MCP Server (streamable-http)
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/coolapk-mcp-pro
Environment=COOLAPK_MCP_TRANSPORT=streamable-http
Environment=COOLAPK_HTTP_HOST=127.0.0.1
Environment=COOLAPK_HTTP_PORT=8000
Environment=COOLAPK_HTTP_TOKEN=换成你的随机长串
ExecStart=/opt/coolapk-mcp-pro/.venv/bin/coolapk-mcp
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now coolapk-mcp
systemctl status coolapk-mcp        # 确认 active (running)
```

### Docker 部署

仓库根自带 `Dockerfile`（`python:3.14-slim` 基础镜像，默认以 streamable-http 形态启动）：

```bash
docker build -t coolapk-mcp .

docker run -d --name coolapk-mcp \
  -p 127.0.0.1:8000:8000 \
  -e COOLAPK_HTTP_TOKEN=<你的随机长串> \
  -v ~/.coolapk-mcp:/root/.coolapk-mcp \
  coolapk-mcp
```

`-e` 只需覆盖 `COOLAPK_HTTP_TOKEN`（传输形态与监听地址已内置默认值）；`-v` 挂载持久化目录后，设备码、登录态与 `auth_blob.bin` 重启容器不丢。同样建议只映射到 `127.0.0.1`，交给 TLS 反代对外。

> **不传 token 容器会「启动即退出」——这是预期的 fail-closed 行为，不是故障**：镜像默认监听 `0.0.0.0`（非回环地址），服务端规定「绑非回环地址必须设置 `COOLAPK_HTTP_TOKEN`」，缺 token 时进程打印「拒绝启动」提示并以非零码退出（`docker logs coolapk-mcp` 可见原因）。这是防止登录态裸奔公网的保护逻辑，补上 `-e COOLAPK_HTTP_TOKEN=<随机长串>` 重新 `docker run` 即可。

### 客户端接入（远程形态）

服务端 URL 统一为 `https://your-domain/mcp`（即反代后的地址加 MCP 端点路径），请求头带 `Authorization: Bearer <token>`。

#### dsh

编辑 dsh profile 配置目录下的 `cordis.patch.yml`：

```yaml
- id: mcp-coolapk-remote
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: coolapk
    transport: streamable-http
    url: https://your-domain/mcp
    headers:
      Authorization: Bearer <token>
    toolCallTimeoutMs: 120000
```

#### Claude Desktop

远程 MCP 建议用官方的 Custom Connector（设置 → Connectors → Add custom connector，填远程 URL），或用 `mcp-remote` 桥接为 stdio：

```json
{
  "mcpServers": {
    "coolapk-remote": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://your-domain/mcp",
               "--header", "Authorization: Bearer <token>"]
    }
  }
}
```

远程接入的具体设置项以 [Claude 官方文档](https://support.claude.com/) 为准。

#### Cursor

编辑项目或全局 `.cursor/mcp.json`，用 `url` 方式直连远程 MCP：

```json
{
  "mcpServers": {
    "coolapk-remote": {
      "url": "https://your-domain/mcp",
      "headers": {
        "Authorization": "Bearer <token>"
      }
    }
  }
}
```

远程字段的最新写法以 [Cursor 官方文档](https://docs.cursor.com/) 为准。

### 图片外链（`/img` 中转）

酷安图床 `image.coolapk.com` 对非酷安客户端返回 `HTTP 567`（防盗链），部分聊天类客户端（如手机语音助手）既打不开这个域名，也没有解码 base64 的能力，只能退化成一串链接。

远程模式为此内置了图片中转：设置 `COOLAPK_PUBLIC_BASE` 后，`coolapk_image_link` 会把酷安图片地址转成 `https://你的域名/img?u=...&exp=...&sig=...` 形式的链接，任何能打开网页的客户端都能直接显示图片。`/img` 默认缩略优先：带尺寸后缀的图先取酷安服务端缩略 `.m.jpg`（大图省约 40% 流量），缩略不可用时自动回落原图；设 `COOLAPK_IMG_THUMBNAIL=0` 关闭。

```bash
# systemd 里加上对外地址（与 COOLAPK_HTTP_TOKEN 一起生效）
Environment=COOLAPK_PUBLIC_BASE=https://mcp.example.com
```

设计要点：

- **签名防滥用**：链接带 `exp`（过期时间戳）与 `sig`（HMAC-SHA256，密钥取 `COOLAPK_HTTP_TOKEN`），任何拿到链接的人都能取图，但改不动参数、也访问不了别的域名——`/img` 只接受 `image.coolapk.com`、`avatar.coolapk.com`、`static.coolapk.com` 三个域名，不接受任意 URL。
- **默认有效期 120 分钟**，单次最长 7 天；图片在服务端不做落盘，取到即转发。
- 只影响图片显示，`/img` 免 Bearer 鉴权（否则 `<img>` 标签加载不了），安全边界完全靠签名与域名白名单。
- 本地 stdio 模式没有对外地址，不生成外链，直接用 `coolapk_read_image` 返回图片即可。

### 服务端登录态

远程形态的服务端登录态与本地完全同源，都在服务器的 `~/.coolapk-mcp/`：

- native 模式：`~/.coolapk-mcp/config.json` 的三键 token（`uid` / `username` / `token`）与设备码（`device_code`），通过 `coolapk login --sms`（短信）/ `--cookie`（Cookie 导入）/ `--adb`（从已连接的安卓设备提取）建立；
- `auth_blob.bin` 签名 blob 仍从酷安安卓 APK 提取：把 APK 改名 `coolapk.apk` 放到 `~/.coolapk-mcp/` 自动提取，或手动放置已有的 `auth_blob.bin`；
- 桌面端 accounts.json 复用（auto 模式）仅在**服务器本机装有酷安桌面端**时生效，纯服务器环境一般用 native 通道。

具体命令与两步验证流程见上文[登录（四通道）](#登录四通道)一节，blob 获取方式见[安装](#安装)一节的前置说明——两者在服务器上同样适用，不赘述。

> ⚠️ **风控提示**：多端 / 多人共享同一部署时，所有请求都从服务器出口 IP 发出，且共用同一设备码——对酷安侧表现为**同一设备高频调用**，比单机 stdio 更容易触发风控。好在缓存与全局限速（默认开启，`COOLAPK_MIN_INTERVAL` / `COOLAPK_NO_CACHE` 可调）会兜底；如出现 `captcha` 风控回落，优先降低多端调用频率，详见[故障排查](#故障排查)。

## 故障排查

### 登录失效（`error_type: auth_expired`）

1. 让 agent 调 `coolapk_login_status` 或运行 `coolapk login --status` 确认失效；
2. 重新登录：桌面端用户重新登录后**直接重试即可**（服务端会在下次调用时自动重建连接）；仍失效则按环境选通道重登——本地桌面推荐 `coolapk login --web`（全平台、含 Python 3.14 均可用，见[登录（四通道）](#登录四通道)）；纯服务器环境用 `--sms` / `--cookie` / `--adb`；
3. stdio 模式下 MCP 服务进程随客户端会话重启，重登后新会话自动带上新登录态。

### 验证码（`error_type: captcha`）

含义：对应接口触发了酷安风控（未登录访问动态详情尤其容易触发，见 酷安 API 社区实测文档（Coolapk-API-Collect） 实测记录）。客户端已自动处理：该端点冷却 600 秒内不再重试；`coolapk_feed_detail` 自动回落到作者动态列表取全文（`fetch_source: author_feed_fallback`）。人工侧能做的：降低调用频率、稍等再试、完成登录（登录态可显著降低未登录风控概率）。

### 图片打不开（HTTP 567）

酷安图片 CDN `image.coolapk.com` 对非官方直连请求返回 HTTP 567（2026-08 实测），把图片 URL 直接丢给浏览器或下载工具大概率拿不到图。请让 agent 使用 `coolapk_read_image` 工具（带 Referer 下载后以图片形式返回给多模态模型）读取关键截图。如果客户端显示不了 base64 图片（例如手机语音助手），改用远程模式的 `coolapk_image_link` 生成签名外链，见上文「图片外链（`/img` 中转）」。

### 其它

- `parse`：多为风控页返回 HTML，按验证码处理思路等待或降低频率；
- `network` / `transient`：客户端已自动退避重试，连续失败再检查本机网络与代理；
- 搜索结果质量差：保持 `strict` 默认开启；需要原始结果时显式传 `strict=false`。

## 目录结构

```text
（仓库根）
├── pyproject.toml
├── coolapk_mcp/
│   ├── server.py       # MCP 服务与 13 个工具
│   ├── cli.py          # coolapk 命令行（搜索 / 详情 / login 等）
│   ├── client.py       # API 客户端（风控回落、全量评论检索）
│   ├── browser_login.py  # --web 网页登录：系统浏览器 CDP 抓 Cookie（回退 pywebview）
│   ├── config.py       # 登录态持久化（~/.coolapk-mcp）
│   ├── throttle.py     # 限速 / 缓存 / 退避重试
│   ├── postfilter.py   # 搜索过滤、去重、评论关键词检索
│   └── auth/           # token v3 签名与设备码
├── docs/
│   └── PHONE-RIKKAHUB.md  # 手机端（RikkaHub 为例）接入指南
├── AGENT-SETUP.md      # 给 AI agent 的自助接入指南
├── NOTICE.md           # 基座来源声明与本仓改动说明
├── CHANGELOG.md        # 版本变更记录
└── LICENSE             # MIT（承自上游）
```

agent 侧使用指南（何时用酷安搜资料、话题映射表、风控礼仪）见 [skills/coolapk-research/SKILL.md](skills/coolapk-research/SKILL.md)。

## 参与贡献

众人拾柴火焰高，欢迎大家提 PR 一起完善！无论是新工具、话题映射表补充、风控经验，还是文档纠错，都欢迎。

1. Fork 本仓并创建特性分支；
2. 保持纯只读定位——任何写操作（点赞 / 回复 / 关注 / 发帖）的改动不会被接受；
3. 提交前跑通冒烟：`.venv\Scripts\coolapk search "Magisk"` 与 `python -m coolapk_mcp.server` 可正常启动；
4. 注释与 commit message 用中文，遵循现有代码风格。

发现酷安接口或风控行为有变化也欢迎开 Issue 反馈实测记录。

## 许可

MIT（见 [LICENSE](LICENSE)），基座来源与改动声明见 [NOTICE.md](NOTICE.md)。
