# 来源声明（NOTICE）

本仓库代码基于以下开源项目修改：

- **基座**：zjy2968466837/coolapk-mcp（2026-10-24 master 快照）
- **上游**：Lniosy/coolapk-mcp（MIT License，见本仓 LICENSE）
- **本 fork 增强来源**：数盟设备码体系（auth/device.py）、风控规避管线（throttle.py）、搜索过滤（postfilter.py）、详情智能回落与全量评论检索（client.py / models.py）均继承自 zjy2968466837/coolapk-mcp

依 MIT 许可平铺引入，不保留上游 git 历史；本仓库为其独立发布与继续演进版本。

## 本仓库相对基座的改动（0.2.0，2026-10-31）

1. 登录态 auto 模式：优先读桌面端 %APPDATA%/com.coolapk.desktop/accounts.json 的 SESSID 完整串（扩展 _get_cookie_header），无效回落原生三键 token
2. 设备码优先复用 accounts.json 的 deviceCode
3. 失效检测（结构化 auth_expired）+ 重登 CLI：login --web（pywebview）/ --sms / --cookie / --adb（从安卓端 shared_prefs 提取）
4. 移除写操作（点赞/回复/关注），纯只读
5. 新增工具：coolapk_read_image（带 Referer 下载返回图片消息）、coolapk_login_status、coolapk_topic 增话题下动态、coolapk_get_url 只读透传
6. 输出按工具字段白名单裁剪

## 文档与配套（2026-10-31）

- 新增 README.md（定位 / 安装 / 登录四通道 / 工具一览 / 配置项 / Agent 接入 / 远程部署 / Docker 部署 / 故障排查）与 CHANGELOG.md（各版本相对基座的增强清单）；0.3.0 起 Agent 接入章按 stdio / 远程两种形态重构，新增 dsh / Claude Desktop / Cursor 三端接入示例
- 新增 skills/coolapk-research/SKILL.md（agent 使用指南：分类 → 话题映射表、工具调用策略、风控礼仪）
- 本文件与基座许可信息保持不变，仅追加改动说明

## 版本里程碑

- **0.3.0（2026-10-31）**：新增远程部署能力——streamable-http 传输（`COOLAPK_MCP_TRANSPORT=streamable-http`）+ Bearer Token 鉴权（`COOLAPK_HTTP_TOKEN`，非回环地址强制）；因公网 IP 直连会被 FastMCP Host 白名单误判，显式关闭 DNS rebinding 防护（`enable_dns_rebinding_protection=False`），以强制 Bearer + TLS 反代兜底；提供 Dockerfile 与远程部署文档；Agent 接入章按 stdio / 远程两种形态重构
- **0.3.1（2026-10-31）**：文档修订——README 快速启动块统一为 Linux 服务器写法并标注、补 Docker fail-closed 行为说明、注明 uvicorn workers 必须为 1（进程内限速 / 缓存不共享）、安全节备注 Host 校验已关闭、`login --web` 的 Python 3.14 不可用表述按 pythonnet 实测校准、故障排查按 Python 版本分流重登通道
