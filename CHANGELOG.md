# 更新日志

## 0.6.7（2026-11-04）

### 新增

- `coolapk_read_image`（本地与远程通用）默认缩略优先：带尺寸后缀的图先取酷安服务端缩略 `.m.jpg`（长边约 1080px，仍在多模态模型视觉窗口内，读图内容不受影响），大图流量省约 70~80%，上游非 200 自动回落原图。

### 变更

- `COOLAPK_IMG_THUMBNAIL` 现在同时控制 `coolapk_read_image` 与 `/img` 中转（默认 `1` 开启，设 `0` 关闭）。

## 0.6.6（2026-11-04）

### 新增

- `coolapk_feed_detail` 输出新增 `share_url`（带分享码的网页链接）：人在浏览器打开可看全文与图片，不经过服务器，适合把帖子分享到聊天窗口。
- `/img` 图片中转默认缩略优先：带尺寸后缀的图先取酷安服务端缩略 `.m.jpg`（大图约省 40% 流量），上游非 200 自动回落原图。

### 变更

- 新增环境变量 `COOLAPK_IMG_THUMBNAIL`（默认 `1` 开启，设 `0` 关闭缩略优先）。

## 0.6.5（2026-11-03）

### 新增

- **评论区金矿**（`coolapk_feed_detail` 三新参数）：
  - `comments_focus`：`smart`（楼主补充 + 热门评论合并，找工具/资源首选）/ `author`（只看楼主）/ `hot`（热门评论）/ `all`（等价 all_replies），返回 `comments_focus` 元数据（各通道条数与总评论数）。
  - `with_history`：附正文修改历史（作者每次编辑的快照，看版本演进）。
  - `expand_sub_replies`：深度展开楼中楼（对回复数多的父评论翻页补全，楼主答疑常在深处；额外发请求，默认关）。
- **评论链接提取**：每条回复自动带 `links` 字段——网盘（夸克/百度/阿里/123pan/MEGA/天翼/移动云盘/UC/蓝奏）与仓库（GitHub/Gitee）链接去重保序，工具帖的下载地址在评论区也能直接拿到。
- 新环境变量 `COOLAPK_MAX_REPLY_PAGES`（默认 30，钳位 1-100）：大帖全量评论翻页上限可调。
- `docs/PHONE-RIKKAHUB.md`：手机端接入指南（以 RikkaHub 为例，配图文截图），从拿到地址和 Token、导入 `mcpServers` JSON、逐项核对服务器配置、四问验收，到「图片为什么是破图」必须走 `coolapk_image_link`，附 7 条常见故障与 4 个手机客户端对照

### 变更

- 客户端新增四个只读方法：楼主评论（`fromFeedAuthor`）、热门评论（`listType=popular`）、楼中楼游标翻页（`firstItem/lastItem`）、修改历史（`changeHistoryList`）。
- `README.md` 工具表 `coolapk_feed_detail` 行补三个新参数说明；环境变量表补 `COOLAPK_MAX_REPLY_PAGES`；`skills/coolapk-research/SKILL.md` 语义搜索策略链第 6 步改为「评论区金矿」指引（smart 首选 + links 字段 + with_history/expand_sub_replies 场景）。

### 文档

- `README.md`：开头新增「接入哪些客户端」一节，明确「凡是支持 MCP 的客户端都能接入」——电脑端编程工具看 `AGENT-SETUP.md`（可让 AI 带你跑通），手机端 AI 助手看 `docs/PHONE-RIKKAHUB.md`；首句客户端范围从「Claude Desktop / Cursor / dsh」扩为「任何支持 MCP 的 AI 助手」；目录结构补 `docs/` 与 `AGENT-SETUP.md`
- `AGENT-SETUP.md`：新增「先判定场景再问问题」决策表（电脑本地 / 手机+有服务器 / 手机+无服务器 / 多台电脑 / Q1 与 Q3 矛盾五种情况各自怎么走）；「登录方式」一问改为按部署形态分支（服务器场景不再问桌面端）；「客户端」一问补辨析表（Claude Code vs Claude Desktop vs 网页版、Cursor/Windsurf、手机三款、未知客户端怎么问）；A5 验收补各客户端的配置核对命令（`dsh --dump-config` / `claude mcp list`）；手机 AI 助手硬规则增加指向 `docs/PHONE-RIKKAHUB.md` 的链接

## 0.6.1（2026-11-03）

网页登录不再依赖 pythonnet，Python 3.14 也能用；同时补齐给 agent 的自助接入文档。

### 新增

- `AGENT-SETUP.md`：面向 agent 的自助接入指南（单文件），AI 读完可主动询问「本机还是服务器 / 用哪种登录 / 有没有装桌面端」，再一步步带用户装上并跑通；含 8 个客户端一句话清单与 dsh / Claude Desktop / RikkaHub 三个完整配置片段
- `coolapk login --web` 改用系统浏览器 + Chrome DevTools Protocol 读 Cookie：启动 Chrome/Edge（独立临时 profile）→ 用户正常登录 → 通过 `Storage.getCookies` 取回 Cookie 并落盘；不依赖 pythonnet，**Python 3.14 也可用**
- 环境变量 `COOLAPK_CDP_PORT`：连接已在运行的浏览器（需带 `--remote-debugging-port`），用于无法自动拉起浏览器的环境
- 环境变量 `COOLAPK_BROWSER`：显式指定 Chrome/Edge 可执行文件路径
- 新增依赖 `websocket-client`（纯 Python，无需编译）

### 变更

- `--web` 的原有 pywebview 实现保留为兜底：仅当本机找不到浏览器时才回退
- 找不到或拉不起浏览器时，输出更明确的替代方案（指定 `COOLAPK_BROWSER` / 用 `COOLAPK_CDP_PORT` 连接 / 手动 `--cookie`）
- README 「Agent 接入」章节压缩为指针，详细流程移入 `AGENT-SETUP.md`；`skills/coolapk-research/SKILL.md` 与 `AGENT-SETUP.md` 互链分工

## 0.6.0（2026-11-01）

远程形态的图片显示：解决「聊天类客户端既打不开酷安图床、也解不了 base64」的最后一公里。

### 新增

- `coolapk_image_link` 工具：把酷安图片地址转成 `https://<你的域名>/img?u=...&exp=...&sig=...` 形式的签名外链，任何能打开网页的客户端（手机语音助手、IM 机器人的富文本等）都能直接显示图片
- `/img` 图片中转端点：校验 HMAC 签名与过期时间后，带酷安客户端 UA 与 Referer 去图床取图并转发，绕开 `image.coolapk.com` 对非客户端请求的 HTTP 567 防盗链；只接受 `image.coolapk.com`、`avatar.coolapk.com`、`static.coolapk.com` 三个域名，不做任意 URL 代理
- 新增环境变量 `COOLAPK_PUBLIC_BASE`：远程部署的对外访问前缀，签名链接基于它生成；未设置时 `coolapk_image_link` 会明确提示改用 `coolapk_read_image`

### 变更

- 工具总数 12 → 13；README 补充「图片外链（`/img` 中转）」章节与故障排查指引

### 安全说明

- 签名密钥取 `COOLAPK_HTTP_TOKEN`，链接默认 120 分钟过期、单次最长 7 天；`/img` 不校验 Bearer（否则 `<img>` 标签无法加载），边界靠签名 + 域名白名单

## 0.5.0（2026-10-31）

作者质量锚定：解决「帖子火 ≠ 作者权威」「热门主题搜不全」两个实测暴露的问题。

### 新增

- `coolapk_author` 作者画像工具：返回 `author_tier`（`top`/`expert`/`active`/`normal`）、`is_expert`、`fans_per_feed`（粉丝/帖数，衡量精发度）、`expertise`（从签名推断的专长领域），用于判断「这人是不是该领域大佬」
- `coolapk_author_digest` 作者深挖聚合工具：一次抓取多页该作者的帖子（默认 2 页、上限 5 页），按主题关键词过滤并按热度排序，返回完整攻略链——实测「莫离然然」的解锁相关前 6 名是 v1.4→v2.2 工具与解锁节 1.0/2.0 全系教程
- 新增 `scoring` 模块：`post_score`（点赞 + 评论×3 + 转发×2）、`discussion_ratio`（评论/点赞）、作者分级阈值（top ≥3 万粉且 ≥10 级，expert ≥1 万粉且 ≥8 级，active ≥1000 粉且 ≥5 级）、`infer_expertise` 关键词分类（解锁/Root、刷机/救砖、模块/内核、美化/主题、省电/性能）
- 帖子类结果统一带 `post_score` 与 `discussion_ratio`（搜索、用户动态、话题动态、作者聚合），可直接用于排序与筛选
- SKILL.md 新增「作者质量锚定」章节：判断教程可信度的四步法、典型解锁 BL 检索链路、召回瓶颈说明（全站搜帖召回不全，热门主题优先走话题通道）

### 变更

- `coolapk_user_feeds` 支持多页聚合与质量筛选：新增 `pages`（1-5）、`keyword`（空格分词任一命中）、`sort`（`default`/`hot`/`latest`），返回结构改为包装对象（`uid`/`pages`/`fetched`/`matched`/`sort`/`keyword`/`feeds`）
- 作者多页聚合按 `id` 去重：实测置顶帖会因 `isIncludeTop=1` 在每页重复出现，3 页 60 条中有 1 条重复

## 0.4.0（2026-10-31）

### 新增

- `coolapk_search` 新增 `sort` 参数（`hot`/`reply`/`default`），feed 搜索默认 `hot`：实测默认综合排序多为低互动点评，热门排序直接返回高赞干货帖
- 新增 `coolapk_suggest` 搜索联想词工具：把口语化需求改写成酷安习惯用词
- `coolapk_topic` 话题动态默认按热度排序（点赞 + 评论×3，`hot_first=false` 可关）：酷安话题接口不支持服务端排序参数，改为客户端重排
- 搜索结果热度字段（`like_num`/`reply_num`/`forward_num`）零值不再省略：AI 可依据热度判断结果质量并筛选
- SKILL.md 新增语义搜索策略链：拆词 → 话题优先 → 联想词改写 → 热度排序

本文件记录本仓库相对基座的增强与修复。基座为 zjy2968466837/coolapk-mcp（2026-10-24 master 快照，承自 Lniosy/coolapk-mcp，MIT License），引入时的基座能力见 [NOTICE.md](NOTICE.md)。

## 0.3.1（2026-10-31）

第二轮全面审查（5 个独立维度：HTTP 传输与安全 / 部署物 / 核心代码回归 / 文档一致性 / 传输层动态测试）后的集中修复。

### 修复

- 登录失效被误报为验证码：403（message 为 `captchaRequired`）的风控响应永远先命中验证码分支，导致 `error_type=captcha` 误导并写入 600 秒负缓存；现 403 判定优先，验证码判定收窄为中文「验证码」字样
- `get_url` 黑名单误伤 7 个只读端点（`/v6/vote/commentList`、`/v6/collection/*`、`/v6/favorite/list`、`/v6/notification/like`、`/v6/user/blackList`）：单段泛词（`/like` `/vote` 等）改为相邻两段形态匹配；反向补齐 8 个此前漏拦的写端点（blacklist 族、`followTag`/`unFollowTag`、`unFavorite`、`sendVcode`），`/v6/account/` 仅放行 `checkLoginInfo`/`requestValidate`
- 纯图帖（无 `content`/`feed_type` 字段）落入用户白名单被裁掉 `id`/`images` 等字段：裁剪结果异常时回落原始条目
- 重试耗尽后误解析末次响应体：429 + HTML 风控页被误报 `error_type=parse`，现按末次状态码构造 `transient`/`http` 错误
- HTTP 错误 payload 新增 `code` 字段（酷安业务错误码），AI 客户端可程序化区分登录失效与风控
- Bearer 鉴权改常量时间比较（`hmac.compare_digest`）且 scheme 大小写不敏感（RFC 9110）；`COOLAPK_HTTP_PORT` 非数字改为友好退出；回环判定支持 `::1` 字面量
- 登录态过期后自动重建客户端：HTTP/stdio 服务进程收到 `auth_expired` 后下次调用重读登录态，桌面端重新登录后直接重试即可生效（无需重启服务进程）
- `COOLAPK_MAX_RETRIES` 上限钳位 8（防指数退避溢出）、`status` 布尔值误判、图片魔数未知时 MIME 误报、空 `rid` 去重、索引/配置文件非 dict 顶层防护

### 变更

- `mcp` 依赖下限抬至 1.30（`TransportSecuritySettings` 顶层导入需要）；显式声明 `click` 依赖（此前靠传递依赖）；补充 `[build-system]`
- Dockerfile 新增 `HEALTHCHECK`（urllib 探活 `/health`）、修正过时注释；`.dockerignore` 清理残留

### 文档

- README「快速启动」统一为 Linux 服务器写法；Docker 节补 fail-closed 说明；注明 uvicorn workers 必须为 1 与 DNS rebinding 校验关闭的取舍
- `login --web` 的 pythonnet 表述校准（锁 3.2.1，Python 3.14 运行时初始化失败，推荐 3.12 或改用 `--sms`/`--cookie`）；故障排查 `auth_expired` 按 Python 版本分流
- SKILL.md 修复断链与「重登后无需重启」的过时表述；NOTICE.md 补 `--adb` 通道与版本里程碑

## 0.3.0（2026-10-31）

### 新增

- 远程 streamable-http 传输：`COOLAPK_MCP_TRANSPORT=streamable-http`，可部署到云服务器供多端直连（默认仍为 stdio，不影响现有用户）
- HTTP 模式内置 Bearer Token 鉴权（`COOLAPK_HTTP_TOKEN`）：绑定非回环地址且未设 token 时拒绝启动；`/health` 健康检查免鉴权
- HTTP 启动时登录态检查：未检测到登录态时在 stderr 打印 native 登录通道指引（不拒绝启动）
- 显式声明 uvicorn 依赖
- 新增 Dockerfile 与 README「远程部署」章（TLS 反代 / systemd / 客户端 URL 直连接入）
- README 重构接入文档：新增「Agent 接入（给 harness 的自助指引）」节——六步自助流程（前置 → 安装 → 登录 → 注册 → 验证 → 学会使用）+ DeepSeek Harness / Claude Desktop / Cursor 三个完整配置示例，任意 agent 可照此自行完成接入，原「三端接入」节并入其中

### 修复

- 关闭 FastMCP 的 DNS rebinding Host 校验（`transport_security=False`）：该校验按 Host 头白名单拦截，客户端用公网 IP 直连服务时合法请求被误判 421 Misdirected Request；Bearer 鉴权已由本服务强制，防护足够

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
