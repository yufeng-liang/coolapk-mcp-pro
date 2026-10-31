# 更新日志

本文件记录本仓库相对基座的增强与修复。基座为 zjy2968466837/coolapk-mcp（2026-10-24 master 快照，承自 Lniosy/coolapk-mcp，MIT License），引入时的基座能力见 [NOTICE.md](NOTICE.md)。

## 0.2.0（草案）

### 新增

- 登录态 auto 模式：优先读取酷安桌面端 `%APPDATA%\com.coolapk.desktop\accounts.json` 中 `lastLoginUid` 对应账户的完整 Cookie（含 SESSID），无效时回落 `~/.coolapk-mcp/config.json` 原生三键 token；设备码优先复用 accounts.json 的 `deviceCode`
- 登录失效检测：受登录态影响的调用失败时返回结构化 `auth_expired` 错误（`error_type` 字段），供 agent 区分风控与失效
- 重登 CLI：`coolapk login --web`（网页登录）/ `--sms`（短信验证码）/ `--cookie`（Cookie 导入）
- 新增工具：`coolapk_read_image`（带 Referer 下载图片，解决 image.coolapk.com 直连 HTTP 567）、`coolapk_login_status`、`coolapk_get_url`（只读 GET 透传）；`coolapk_topic` 支持 `with_feeds` 拉取话题下动态
- agent 使用 skill：`skills/coolapk-research/SKILL.md`（分类 → 话题映射表、工具调用策略、风控礼仪）

### 变更

- 移除基座的写操作（点赞 / 回复 / 关注 / 发帖 / 私信发送），定位收敛为纯只读资料检索
- 工具输出按字段白名单裁剪，压缩返回体积

### 文档

- 新增 README.md：定位、安装、登录三通道、9 工具一览、配置项、dsh / Claude Desktop / Cursor 三端接入示例、故障排查
- 新增 CHANGELOG.md（本文件）
- NOTICE.md 追加 0.2.0 改动说明

### 修复

- 登录态本地预检覆盖所有请求：`_check_local_auth()` 统一挂在 `_request()` 入口（跳过 `/v6/auth/` 端点自身），登录失效时所有工具直接返回结构化 `auth_expired`，不再等发出请求后被服务端拒绝
