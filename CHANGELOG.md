# 更新日志

本文件记录本仓库相对基座的增强与修复。基座为 zjy2968466837/coolapk-mcp（2026-10-24 master 快照，承自 Lniosy/coolapk-mcp，MIT License），引入时的基座能力见 [NOTICE.md](NOTICE.md)。

## 0.3.0（2026-10-31）

### 新增

- 远程 streamable-http 传输：`COOLAPK_MCP_TRANSPORT=streamable-http`，可部署到云服务器供多端直连（默认仍为 stdio，不影响现有用户）
- HTTP 模式内置 Bearer Token 鉴权（`COOLAPK_HTTP_TOKEN`）：绑定非回环地址且未设 token 时拒绝启动；`/health` 健康检查免鉴权
- HTTP 启动时登录态检查：未检测到登录态时在 stderr 打印 native 登录通道指引（不拒绝启动）
- 显式声明 uvicorn 依赖
- 新增 Dockerfile 与 README「远程部署」章（TLS 反代 / systemd / dsh、Claude Desktop、Cursor 三端接入）

## 0.2.0

### 新增

- 登录态 auto 模式：优先读取酷安第三方桌面端（github.com/coolapk-desktop/coolapk-desktop，社区维护的非官方客户端）`%APPDATA%\com.coolapk.desktop\accounts.json` 中 `lastLoginUid` 对应账户的完整 Cookie（含 SESSID），无效时回落 `~/.coolapk-mcp/config.json` 原生三键 token；设备码优先复用 accounts.json 的 `deviceCode`
- 登录失效检测：受登录态影响的调用失败时返回结构化 `auth_expired` 错误（`error_type` 字段），供 agent 区分风控与失效
- 重登 CLI：`coolapk login --web`（网页登录）/ `--sms`（短信验证码）/ `--cookie`（Cookie 导入）/ `--adb`（从已连接的 Android 设备提取）
- 新增工具：`coolapk_read_image`（带 Referer 下载图片，解决 image.coolapk.com 直连 HTTP 567）、`coolapk_login_status`、`coolapk_get_url`（只读 GET 透传）；`coolapk_topic` 支持 `with_feeds` 拉取话题下动态
- agent 使用 skill：`skills/coolapk-research/SKILL.md`（分类 → 话题映射表、工具调用策略、风控礼仪）

### 变更

- 移除基座的写操作（点赞 / 回复 / 关注 / 发帖 / 私信发送），定位收敛为纯只读资料检索
- 工具输出按字段白名单裁剪，压缩返回体积

### 文档

- 新增 README.md：定位、安装、登录四通道、9 工具一览、配置项、dsh / Claude Desktop / Cursor 三端接入示例、故障排查；开篇新增「准备清单」——明确酷安官方没有桌面端、登录态依赖的第三方桌面端项目（github.com/coolapk-desktop/coolapk-desktop）来源与获取方式、有/无桌面端两条准备路径
- 新增 CHANGELOG.md（本文件）
- NOTICE.md 追加 0.2.0 改动说明

### 修复

- `auth_expired` 错误提示补充桌面端重登路径（在第三方桌面端重新登录后重试即可，auto 模式自动复用）；`login --web` 在文档中标注 Python 3.14 已知不可用（pythonnet 限制）

- 登录态本地预检覆盖所有请求：`_check_local_auth()` 统一挂在 `_request()` 入口（跳过 `/v6/auth/` 端点自身），登录失效时所有工具直接返回结构化 `auth_expired`，不再等发出请求后被服务端拒绝
- 话题搜索 `tag` 参数未 URL 编码：`#话题名#` 中的 `#` 截断 query 导致搜索必然查空，现已 `quote` 编码
- 风控响应静默当成功：`{status:1004/-415, data:null}` 形态此前绕过错误分支静默返回空结果，且 `data:null` 被写入缓存造成缓存投毒，现已按风控错误抛出并跳过缓存
- 顶层 list 响应崩溃：搜索类接口返回顶层 list 时在错误检查段抛 `AttributeError`，现已按预期形态直通
- `get_url` 写端点黑名单双重编码绕过：`%64igg`/`%2564igg` 解码后即写端点 `/digg`，现已循环解码后按路径段匹配；同时修复裸子串匹配误伤 `/v6/feed/replyList`、`/v6/message-like` 只读端点的问题
- 敏感字段剔除不足：`accessToken`/`sessionId`/`userToken`/`secret` 等变体键与嵌套 `config` 键此前可透出，现改为整键精确 + 关键子串双判并递归剔除
- `user_profile` / 搜索用户结果粉丝数丢失：输出白名单键与模型 dump 键错配（`fans` vs `fans_num`），粉丝/关注/动态数被永久裁掉
- 搜索广告标记污染缓存：过滤标记原地写入缓存对象导致二次命中重复计数，现浅拷贝后操作
- 配置文件与索引文件非原子写：中断可能产生半截 JSON 被静默清空，现改临时文件 + `os.replace`
- SESSID 失效检测漏检分号后带空格的变体（`; SESSID=deleted`），统一交给按键解析的 `has_valid_sessid` 判定
- 桌面端 accounts.json 结构损坏（顶层 null/list 等）导致 `AttributeError` 崩溃，现按无登录态处理
- `COOLAPK_MAX_RETRIES` 负值导致零次请求，现钳位非负、非法值回落默认 2
- blob 缺失报错引用不存在的 `coolapk auth --extract` 子命令，改为给出可执行的修复路径（APK 自愈提取 / 手动放置 blob），并新增进程内 blob 内存缓存
