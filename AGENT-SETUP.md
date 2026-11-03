# AGENT-SETUP.md — AI / Agent 自助接入指南

> **读者是 AI agent**：如果你是运行在某个 harness / MCP 客户端里的 AI，正在帮用户安装并接入 coolapk-mcp-pro，本文就是写给你的。按步骤执行，读完即可一步步装好、跑通、验收。
>
> 本文基于 **v0.6.1**（`tools/list` 应恰好返回 **13 个工具**）。工具数量以 `tools/list` 实际返回为准；行为细节以 `coolapk_mcp/` 源码为准。

---

## 给 AI 的三条硬规则

1. **能跑 shell，就走「可执行路径」**：clone 仓库、建 venv、pip 安装、写客户端配置、跑验收命令，都由你直接执行，不要把命令转述给用户让他手敲。**不能跑 shell**（或只读对话环境），就把本文相应步骤原文转述给用户，让他逐条点按，你在对话里指挥与核对结果。
2. **用户没有云服务器 → 一律本地 stdio**。只有用户明确说「多台设备共用」「手机上的 AI 助手也要连」时，才走远程 streamable-http（需要一台 Linux 云服务器）。不确定时按本地 stdio 处理，不要主动劝用户买服务器。
3. **手机 AI 助手（超级小爱、RikkaHub、Kelivo 等）只能连远程形态**——手机客户端没有本地进程可拉起，stdio 对它们不存在。用户若只有手机端需求而没有云服务器，先如实告知这个前提，再问是否愿意准备一台服务器；不愿意就到此为止，不要强行接入。**手机端的具体步骤（导入 JSON、逐项核对、看图兜底、常见故障）见 [docs/PHONE-RIKKAHUB.md](docs/PHONE-RIKKAHUB.md)**，不要自己现编 UI 路径。

---

## 第 1 步 · 自检环境（先跑命令收集事实，别问用户能自己查到的）

能跑 shell 的 agent，直接跑下面的命令（能跑哪个跑哪个，失败不阻塞）：

```bash
# 操作系统
uname -a          # Linux / macOS
ver               # Windows（cmd）；PowerShell 用 $env:OS

# Python 版本与路径（本项目要求 >= 3.10，3.14 也可以）
python3 --version         # Linux / macOS
py -0p                    # Windows：列出所有已装 Python
python -c "import sys; print(sys.executable)"   # 当前 python 的真实路径

# 历史配置是否已存在（之前接过就不用重装）
ls ~/.coolapk-mcp/        # 关注 config.json（登录态）、auth_blob.bin（签名 blob）
cat %APPDATA%\com.coolapk.desktop\accounts.json  # 仅 Windows：桌面端登录态
ls "%APPDATA%\com.coolapk.desktop\"              # Windows cmd 写法

# 浏览器（网页登录 --web 依赖 Chrome 或 Edge）
where chrome msedge 2>nul          # Windows
ls /Applications | grep -iE "chrome|edge"   # macOS
which google-chrome chromium microsoft-edge 2>/dev/null   # Linux
```

**收集到什么就记什么**，对照下表判断起点：

| 检查项 | 结果 | 影响 |
| - | - | - |
| `~/.coolapk-mcp/config.json` 存在 | 已有登录态 | 路线 A 的登录（A2）可跳过，直接验收 |
| `accounts.json` 存在（仅 Windows） | 桌面端已装且登录过 | auto 模式自动复用，登录一步不用做 |
| `auth_blob.bin` 存在 | 签名 blob 就绪 | 不用再准备 APK |
| Python ≥ 3.10 | 满足 | 直接安装 |
| Python < 3.10 或无 Python | 不满足 | 先让用户装 Python 3.12（或任何 ≥3.10 版本） |
| 有 Chrome / Edge | 网页登录可用 | 登录通道②可用 |

不能跑 shell 的 agent：跳过本步的自检命令，把第 3 步的问题原样问用户，并在用户回答里判断以上条件（**系统版本仍要当面问一句**，否则 macOS 用户会被引导去装桌面端）。

---

## 第 2 步 · 先判定场景，再问问题

**先按下面这张表判断用户属于哪种情况，再决定问什么、走哪条路线。** 判定靠事实（第 1 步自检到的 OS / 已装客户端 / 有没有服务器），不要靠猜。

> ⛔ **场景没判定清楚之前，不要开始 clone / 装 venv / 写配置**——形态选错等于白装一遍（本地装完发现是手机客户端要连、服务器装完发现用户只想本机用）。

| # | 用户情况 | 判定依据 | 走哪条 |
| - | - | - | - |
| 1 | 只在自己电脑上用，客户端装在电脑上 | Q1=①，Q3 是电脑客户端 | 路线 A（本地 stdio），配置照 A4 对应那段 |
| 2 | 手机 AI 助手 + 有一台 Linux 云服务器 | Q3 是 RikkaHub / Kelivo / 超级小爱，且用户说有服务器 | 路线 B（远程），手机端步骤另看 [docs/PHONE-RIKKAHUB.md](docs/PHONE-RIKKAHUB.md) |
| 3 | 手机 AI 助手 + **没有**服务器 | Q3 是手机客户端，用户说没有服务器、也不想买 | **到此为止**：如实说「手机端跑不了本地进程，必须有一台服务器才能接」，不要硬接、不要改成本地 stdio 糊过去 |
| 4 | 多台电脑共用（家里 + 公司 / 笔记本 + 台式） | Q1=② | 路线 B，电脑客户端用 B5 的远程片段 |
| 5 | 用户说「我就想在这台电脑用」，但 Q3 选了手机客户端 | Q1 与 Q3 矛盾 | **停下来澄清**：「手机客户端连不了本机进程，你是想在这台电脑上用，还是想在手机上用？两者需要不同的部署方式」——不要自己替用户改答案 |

> 判定顺序：**先看客户端类型（Q3），再看部署形态（Q1）**。因为「客户端能不能跑本地进程」是硬约束，会反过来决定部署形态。

---

## 第 3 步 · 问用户问题（原文可直接复制）

> 只问自检查不到的（比如「有没有云服务器」「愿不愿意装桌面端」）。有面板能力的客户端弹选择面板：**dsh 用 `ask_user_question` 工具、Claude Code 用 `AskUserQuestion`**，其余客户端用纯文本编号选项。用户跳过的问题按「默认」处理，视为已确认，之后不要再重复问。

**Q1 · 部署形态**（默认 ①）

> 「coolapk MCP 装在哪？
> ① 只在这台电脑用（本地安装，推荐）
> ② 多台设备 / 手机上的 AI 助手也要共用（需要一台 Linux 云服务器）
> ③ 不确定，帮我选」

判定：① → 路线 A；② → 路线 B；③ → 按硬规则 2 默认路线 A。

**Q2 · 登录方式**（按 Q1 分开问，**别在服务器场景问「装桌面端」**）

Q1 = ① 本地时问：

> 「酷安登录怎么弄？
> A 我安装酷安第三方桌面端并登录一次（**仅 Windows 推荐**，之后全自动）
> B 不装桌面端，用手机配合（浏览器 / 短信验证码 / ADB）」

判定：A → 通道①（**仅 Windows 有效**；用户是 macOS / Linux 就直接给 B，别让他白装）；B → 通道②③④⑤按顺序试，哪个成功用哪个。

> 系统不能靠猜：能跑 shell 就查（第 1 步），不能跑就明确问一句「你的电脑是 Windows 还是 macOS / Linux」。

Q1 = ② 服务器时**不要问上面这两个选项**（Linux 无图形界面，桌面端装了也没用），直接告知：

> 「服务器上没有图形界面，登录态只能靠两种方式建立：① 短信验证码（手机收码，我带你走两步）② Cookie 导入（从你已登录的浏览器里复制）。你想用哪种？」

判定：① → B2 短信通道；② → B2 Cookie 通道。

**Q3 · 客户端**

> 「你在用哪个 AI 客户端？
> dsh / Claude Code / Claude Desktop / Cursor / Cline / RikkaHub / Kelivo / 超级小爱 / 其他」

判定：决定路线 A 的 A4 或路线 B 的 B5 写哪份配置；RikkaHub / Kelivo / 超级小爱 → **只能路线 B**。

用户只说了品牌名、分不清具体产品时，按下表追问一句（选错会写出无效配置）：

| 用户说的 | 实际要确认的 | 判据 |
| - | - | - |
| 「Claude」 | Claude Code（终端里跑）还是 Claude Desktop（桌面应用） | 有终端、敲 `claude` 命令的是 Code；有窗口、装的是应用的是 Desktop |
| 「Claude」但其实是 claude.ai 网页 | 网页版**不支持**自定义 MCP 服务器 | 直接告知不支持，让他改用上述任一客户端 |
| 「Cursor / Windsurf / VS Code 插件」 | 走 MCP Servers 配置的 JSON | 结构同 Cursor 的 `mcpServers` |
| 「手机上的」 | 具体哪款（RikkaHub / Kelivo / 超级小爱） | 三款都是仅远程；配置差异见 PHONE-RIKKAHUB.md 的客户端对照 |
| 「其他 / 不知道名字」 | 只问两件事：① 能不能填 `command` + `args`（能 → 本地 stdio）② 能不能填 `url` + `headers`（能 → 远程） | 两个都能 → 优先本地 stdio；只有 url → 远程 |

---

## 路线 A · 本地 stdio（Windows / macOS / Linux 桌面）

适用：Q1 答 ①。MCP 进程跑在用户本机，随客户端会话启动退出。

### A1 · 安装

```bash
git clone https://github.com/yufeng-liang/coolapk-mcp-pro.git
cd coolapk-mcp-pro

# Windows（py 启动器）：
py -m venv .venv
.venv\Scripts\python -m pip install -e .

# macOS / Linux：
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

安装成功的判定：`.venv\Scripts\coolapk --help`（Windows）或 `.venv/bin/coolapk --help`（macOS/Linux）能打印用法。项目提供两个入口命令：`coolapk`（命令行工具：搜索 / 登录）与 `coolapk-mcp`（MCP 服务本体）。

**记住克隆目录的绝对路径**，写客户端配置时要用。

### A2 · 登录（按优先级尝试，一个成功就停）

MCP 服务启动时会自动管理登录态，你只需要用下面任一通道把登录态建立起来。**每一步成功后都跑 `coolapk login --status` 确认**（输出里 `logged_in: true` 且 `uid` 非空即成功）。

| 优先级 | 通道 | 命令 | 前置与说明 |
| - | - | - | - |
| ① | 桌面端复用 | 无需命令（auto 模式自动） | **仅 Windows 生效**。用户装好[酷安第三方桌面端](https://github.com/coolapk-desktop/coolapk-desktop/releases)并登录一次即可。macOS / Linux 实测会退回到一个不存在的路径，**不要指望它**，直接用通道② |
| ② | 网页登录 | `.venv\Scripts\coolapk login --web` | 拉起系统浏览器（Chrome / Edge）打开酷安授权页，用户在窗口里完成登录（含滑块 / 验证码），服务端自动抓取 Cookie。300 秒超时。Windows / macOS / Linux 桌面均可用，**Python 3.14 也可以**（优先走 CDP 读取 Cookie，仅在本机找不到浏览器时才回退 pywebview 窗口）。浏览器装在非标准位置时用环境变量 `COOLAPK_BROWSER` 指定可执行文件路径；已开着带远程调试的浏览器时可设 `COOLAPK_CDP_PORT` 直连。无图形环境（纯服务器）不可用 |
| ③ | ADB 提取 | `.venv\Scripts\coolapk login --adb` | 手机 USB 连接、开 USB 调试，**手机需要 root**（命令内部用 `su` 读酷安的 shared_prefs）。手机未 root 用不了 |
| ④ | 短信登录 | 两步，见下 | 手机收短信验证码。无需 root，无需装桌面端 |
| ⑤ | Cookie 导入 | `.venv\Scripts\coolapk login --cookie "uid=…;username=…;token=…"` | 兜底通道。用户从浏览器 F12 或桌面端日志里复制 Cookie（`SESSID` 可选） |

**短信登录两步流程**（通道④）：

```bash
# 第 1 步：请求验证码（不带 --captcha 时会自动下载图形验证码到临时目录，输出里给出图片路径）
coolapk login --sms --phone <手机号>
# → 让用户看图报出图形验证码，然后带上 --captcha 重跑第 1 步（这才真正发出短信）：

coolapk login --sms --phone <手机号> --captcha <图形验证码>
# → 成功后返回 sessid 与 validate_url（在输出 JSON 的 message 字段），最后：

# 第 2 步：提交短信验证码（sessid 与 validate-url 从上一步 JSON 输出里抄）
coolapk login --sms --code <短信码> --sessid <返回的sessid> --validate-url <返回的message>
```

第 1 步输出 `step: captcha_required` 时，把验证码图片路径告诉用户（或你自己读图识码）。全部成功会输出登录成功的 JSON。

**登录失败的处置**：每条失败输出都是 `{"error": "…"}` JSON，如实转给用户并按「故障回退表」换下一通道。

### A3 · auth_blob 签名数据（token V3 必需，缺了所有请求报错）

- **是什么**：酷安 v3 token 签名依赖一段从安卓酷安 APK `libauth.so` 解出的 base64 blob，服务端首次需要时会自动提取并缓存到 `~/.coolapk-mcp/auth_blob.bin`。
- **两条获取路径**（二选一）：
  1. **APK 自动提取**：拿到任意渠道的酷安安卓 APK，改名 `coolapk.apk` 放到 `~/.coolapk-mcp/`，首次调用时自动提取（提取后 APK 可留作备份，blob 损坏时可自愈重提）；
  2. **复制现成文件**：从任何一台已跑通的环境把 `~/.coolapk-mcp/auth_blob.bin` 复制过来。
- **成功标准**：`ls -la ~/.coolapk-mcp/auth_blob.bin` 显示文件存在、约 **930 字节**。

注意：桌面端安装包（exe / dmg）里提不了 blob——即使走通道①桌面端复用登录，也需要按上面方式准备 blob（除非之前已经放过）。

### A4 · 写客户端配置（按 Q3 的答案选一份）

统一三要素：`command` = venv 的 python 可执行文件绝对路径、`args` = `["-m", "coolapk_mcp.server"]`、路径前缀指向克隆目录。

**dsh**：编辑 profile 配置目录下的 `cordis.patch.yml`，追加：

```yaml
- id: mcp-coolapk
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: coolapk
    transport: stdio
    command: D:/coolapk-mcp-pro/.venv/Scripts/python.exe   # 换成实际路径；macOS/Linux 是 .venv/bin/python
    args:
      - -m
      - coolapk_mcp.server
```

写完用 `dsh --profile web --dump-config` 核对新行已合入，重启 dsh 会话生效。

**Claude Desktop**：编辑 `%APPDATA%\Claude\claude_desktop_config.json`（Windows；JSON 内路径用 `\\` 转义）：

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

**Cursor**：编辑项目或全局 `.cursor/mcp.json`（正斜杠路径即可）：

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

**Claude Code**：`claude mcp add coolapk -- D:/coolapk-mcp-pro/.venv/Scripts/python.exe -m coolapk_mcp.server`（具体语法以 Claude Code 官方文档为准）。

**Cline**：MCP 服务器设置（结构同 Cursor 的 mcpServers），command / args 同上。

**其他客户端**：按三要素自行适配——支持 MCP 的客户端都能接。

### A5 · 验收

重启 / 重载客户端后，先做一次「配置真的被吃进去了」的核对，再按下方「验收」一节的 5 项硬门槛逐项执行：

| 客户端 | 核对命令 / 位置 | 期望 |
| - | - | - |
| dsh | `dsh --profile <profile> --dump-config` | 输出里能找到 `mcp-coolapk` 这一行，且 `command` / `args` 未丢 |
| Claude Code | `claude mcp list` | 列表里出现 `coolapk`，状态为 connected |
| Claude Desktop / Cursor / Cline | 重启应用 → 设置里的 MCP 列表 | 出现 `coolapk`，工具数 13 |

全部通过后向用户报告结果。

---

## 路线 B · 远程 streamable-http（Linux 云服务器）

适用：Q1 答 ②，或 Q3 答了手机客户端（RikkaHub / Kelivo / 超级小爱）。服务跑在 Linux 服务器上常驻，客户端通过 URL + Bearer Token 远程连接。

### B1 · 安装（服务器上执行）

```bash
git clone https://github.com/yufeng-liang/coolapk-mcp-pro.git /opt/coolapk-mcp-pro
cd /opt/coolapk-mcp-pro
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

### B2 · 服务端登录态

**服务器上不要装桌面端**（Linux 无图形环境，桌面端复用通道无效）。用这两个通道：

- **短信登录（主推）**：同 A2 通道④的两步流程，用 `.venv/bin/coolapk login --sms --phone <手机号>` 开始。没有图形界面也能走完——图形验证码图片会存到临时目录，你可以 `scp` 下来看，或把路径告诉用户让他自己看。
- **Cookie 导入（兜底）**：`.venv/bin/coolapk login --cookie "uid=…;username=…;token=…"`。

`auth_blob` 准备方式同 A3：把酷安 APK 传到服务器 `~/.coolapk-mcp/coolapk.apk`，或从本机 `scp ~/.coolapk-mcp/auth_blob.bin` 上去。确认：`coolapk login --status` 输出 `logged_in: true`。

### B3 · 环境变量与 systemd 常驻

```bash
# 生成随机 Token（记下来，客户端要填）
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

> 🔴 **安全红线：Token 等于这个酷安账号的全部读写能力**。谁拿到 Token 就能以该账号身份调用全部工具。本文与所有示例里一律写 `<TOKEN>` 占位；真实 Token 不要贴进聊天记录、issue、截图、文档。

创建 `/etc/systemd/system/coolapk-mcp.service`：

```ini
[Unit]
Description=Coolapk MCP Server (streamable-http)
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/coolapk-mcp-pro
Environment=COOLAPK_MCP_TRANSPORT=streamable-http
Environment=COOLAPK_HTTP_HOST=127.0.0.1
Environment=COOLAPK_HTTP_PORT=8000
Environment=COOLAPK_HTTP_TOKEN=<TOKEN>
Environment=COOLAPK_PUBLIC_BASE=https://mcp.example.com
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
curl http://127.0.0.1:8000/health   # 应返回 {"status":"ok"}（免鉴权探活端点）
```

环境变量速查：

| 变量 | 必填 | 说明 |
| - | - | - |
| `COOLAPK_MCP_TRANSPORT` | ✅ | `streamable-http`（别名 `http` / `streamable_http`） |
| `COOLAPK_HTTP_HOST` | 建议 `127.0.0.1` | 绑回环由反代对外最安全；绑 `0.0.0.0` 必须配 Token 否则拒启 |
| `COOLAPK_HTTP_PORT` | 默认 `8000` | |
| `COOLAPK_HTTP_TOKEN` | ✅ | Bearer Token，客户端与服务端共用 |
| `COOLAPK_PUBLIC_BASE` | 图片外链需要 | 对外访问前缀（如 `https://mcp.example.com`），设置后 `coolapk_image_link` 才能生成图片链接 |

**不要用 `--workers N` 多进程拉起**：限速、缓存、验证码冷却是进程内状态，多进程互不共享，风控风险成倍上升。一台服务器一个实例。

### B4 · HTTPS 最小上线（实测 10 步内走通）

1. 云服务器安全组放行 80 / 443 端口（8000 不放行，不直连公网）；
2. 完成 B1–B3 部署并确认 `systemctl status coolapk-mcp` 为 active；
3. 免费域名：用 sslip.io——公网 IP 的点换成横线即域名，如 IP `1.2.3.4` → `1-2-3-4.sslip.io`，无需注册；
4. 安装 nginx，反代配置（关键两项：**关缓冲**、**拉长读超时**，MCP 流式响应需要）：

```nginx
server {
    listen 80;
    server_name 1-2-3-4.sslip.io;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;            # MCP 流式响应必须关闭缓冲
        proxy_read_timeout 3600s;       # 长连接工具调用
    }
}
```

5. `certbot --nginx -d 1-2-3-4.sslip.io` 自动签发 Let's Encrypt 证书并改写配置；
6. 验证：`curl https://1-2-3-4.sslip.io/health` 返回 `{"status":"ok"}` 即上线完成。

### B5 · 客户端接入（远程形态）

服务端 MCP 端点统一为 `https://<域名>/mcp`。**注意各客户端 Token 填法差异**：多数客户端 Token 栏只填值（客户端自动加 `Bearer ` 前缀）；dsh 的 headers 里要自己写全 `Bearer <TOKEN>`。

**dsh**（cordis.patch.yml）：

```yaml
- id: mcp-coolapk-remote
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: coolapk
    transport: streamable-http
    url: https://mcp.example.com/mcp
    headers:
      Authorization: Bearer <TOKEN>
    toolCallTimeoutMs: 120000
```

**Cursor**（`.cursor/mcp.json`）：

```json
{
  "mcpServers": {
    "coolapk-remote": {
      "url": "https://mcp.example.com/mcp",
      "headers": { "Authorization": "Bearer <TOKEN>" }
    }
  }
}
```

**Claude Desktop**：远程 MCP 用官方 Custom Connector（设置 → Connectors → 添加自定义连接器，填 URL），或 `mcp-remote` 桥接为 stdio：

```json
{
  "mcpServers": {
    "coolapk-remote": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://mcp.example.com/mcp",
               "--header", "Authorization: Bearer <TOKEN>"]
    }
  }
}
```

**RikkaHub / Kelivo / 超级小爱**：在应用的 MCP / 插件设置里填 URL（`https://<域名>/mcp`）与 Token（只填值），以各应用内说明为准。手机端显示不了 base64 图片，验收时用 `coolapk_image_link` 的外链链路。

---

## 验收（5 项硬门槛，全部通过才算接入完成）

在客户端里（或直接调 MCP）逐项执行：

| # | 验收项 | 通过标准 |
| - | - | - |
| 1 | 拉取工具清单 | `tools/list` 恰好返回 **13 个** `coolapk_*` 工具（search / feed_detail / user_profile / user_feeds / author / author_digest / home / topic / suggest / login_status / read_image / image_link / get_url） |
| 2 | 登录态 | 调 `coolapk_login_status`，返回 `is_logged_in: true` 且 `uid` 与用户预期一致（注意与 CLI `coolapk login --status` 的 `logged_in` 字段名不同） |
| 3 | 真实搜索 | 调 `coolapk_search`（keyword 任选，如 `Magisk 模块`），返回非空列表 |
| 4 | 图片链路 | 本地：`coolapk_read_image` 传入上一步结果里任一帖子的图片 URL，能返回图片；远程：改用 `coolapk_image_link` 生成外链且链接能打开 |
| 5 | 仅远程 | `curl https://<域名>/health` 返回 200 与 `{"status":"ok"}`；不带 Token 请求 `https://<域名>/mcp` 返回 **401** |

**验收通过后向用户报告**：工具数量、登录 uid、冒烟搜索的关键词与结果条数、图片链路结果。**验收不过**：按「故障回退表」定位，修完重跑未通过的那几项。

---

## 故障回退表（现象 → 判定 → 回到第几步）

| 现象 | 判定 | 处置 |
| - | - | - |
| `error_type: auth_expired` | 登录态失效 | 本地：桌面端用户重新登录后**直接重试即可**（服务端下次调用自动重建连接）；仍失效再重启 / 重载 MCP 会话。服务器：回 B2 重新登录 |
| `error_type: captcha` | 触发酷安风控 | 正常现象：服务端已自动冷却 600 秒、`feed_detail` 自动回落作者动态取全文。降低调用频率、完成登录可显著缓解 |
| `403 empty response body` | 桌面端设备签名问题 | 本地：换 A2 通道②③④建立原生登录态，不要只依赖桌面端复用 |
| `HTTP 567`（图片） | 酷安图片 CDN 防盗链 | 用 `coolapk_read_image`（本地）/ `coolapk_image_link`（远程），不要直连图片 CDN |
| `libauth blob 未缓存` | 缺签名数据 | 回 A3：放 `coolapk.apk` 或复制 `auth_blob.bin` 到 `~/.coolapk-mcp/` |
| `Connection refused` | 服务没起来或端口不对 | 远程：`systemctl status coolapk-mcp`、核对端口与反代配置；本地：客户端配置里的 command 路径是否正确 |
| `ModuleNotFoundError: coolapk_mcp` | 用了系统 Python 而非 venv | 回 A1/B1：命令前缀一律用 `.venv` 里的 python / 可执行文件 |
| `Task group is not initialized` | 自己写了 `uvicorn.run` 没跑 lifespan | 用 `coolapk-mcp` 入口命令启动，不要自己拼 uvicorn 脚本 |
| `--web` 报「浏览器无法启动 / 网页登录窗口启动失败」 | 无图形环境、缺浏览器或浏览器受限 | 确认装了 Chrome / Edge，或设 `COOLAPK_BROWSER` 指向可执行文件重试；服务器环境本就不支持，换 `--sms` / `--cookie` |

---

## 客户端适配速查

| 客户端 | 支持形态 | 配置位置 | 一句话要点 |
| - | - | - | - |
| dsh | stdio + 远程 | profile 目录 `cordis.patch.yml` | 改完 `dsh --profile web --dump-config` 核对；远程 headers 里写全 `Bearer <TOKEN>` |
| Claude Code | stdio | `claude mcp add` | 命令行注册，`--` 后接 venv python 与参数 |
| Claude Desktop | stdio + 远程 | `claude_desktop_config.json` | 远程建议官方 Custom Connector 或 `mcp-remote` 桥接 |
| Cursor | stdio + 远程 | `.cursor/mcp.json` | mcpServers 结构，远程直接 `url` + headers |
| Cline | stdio | MCP Servers 设置 | 结构同 Cursor |
| RikkaHub | 仅远程 | 应用内 MCP 设置 | 手机端；图片用 `coolapk_image_link` 外链 |
| Kelivo | 仅远程 | 应用内 MCP 设置 | 同上 |
| 超级小爱 | 仅远程 | 服务配置 | 同上；截图类内容记得转外链 |

---

## 安全红线与维护约定

- **Token = 账号读写能力**：不贴聊天 / issue / 截图，泄露立即在服务器上换新并重启服务；
- **远程必须 TLS**：Bearer Token 是明文凭据，裸 HTTP 公网传输会被中间人截获；8000 端口不直连公网；
- **纯只读**：本服务没有任何写操作（点赞 / 回复 / 关注 / 发帖），客户端接上也不会有——这是特性不是缺陷；
- 工具数量与行为以 `tools/list` 实际返回与 `coolapk_mcp/` 源码为准（登录：`coolapk_mcp/cli.py`；配置：`coolapk_mcp/config.py`；服务与工具：`coolapk_mcp/server.py`；签名 blob：`coolapk_mcp/auth/libauth.py`）；
- 本文基于 v0.6.1（13 个工具）；接口或行为变化时欢迎提 PR 更新本文。装好之后的**使用方法**（怎么搜得准、调用策略、风控礼仪）见 [skills/coolapk-research/SKILL.md](skills/coolapk-research/SKILL.md)。
