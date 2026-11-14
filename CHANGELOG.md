# 更新日志

## 0.7.0（2026-11-14）

### 新增

- 错误响应新增 `retryable` 标记（仅在可重试时为 `true`）：`network` / `transient` / `rate_limit` 可原样重试，`auth_expired` / `risk` / `api` 重试无效，agent 不必靠 error_type 猜。
- 新增 `error_type=risk`：HTTP 403 且响应体是风控/频率文案时归入此类，附 `retry_after`（优先用服务端 `Retry-After`，缺省 120 秒）。此前这类 403 一律报「登录态已失效」，会把用户误导去重新登录。
- 新增 `error_type=rate_limit`：HTTP 429 单独归类并解析服务端 `Retry-After`；建议等待时间超过退避上限（`COOLAPK_MAX_BACKOFF`，默认 30 秒）时不再重试，直接交回调用方，避免加重封禁；未超过时重试前的等待取「退避曲线」与 `Retry-After` 的较大值，不再只按本地曲线白撞一次限流窗口。
- 新增 4 个工具，补齐此前只能靠 `coolapk_get_url` 手拼路径的能力：`coolapk_board`（21 个板块，支持板块 id / 中文名）、`coolapk_secondhand`（二手市场，可按品牌过滤）、`coolapk_app_detail`（应用详情）、`coolapk_notify`（@/评论/点赞/关注/私信，可选未读数）；工具总数 13 → 17。`coolapk_board` 的 `board` 参数同时接受板块 id、中文名与 page_name；`coolapk_notify` 条目的 `dateline` 与其它工具同口径（ISO8601+08:00）。
- `coolapk_user_feeds` / `coolapk_author_digest` 新增 `keyword_mode=any|all`：`all` 要求关键词全部命中，用于精确圈定同一主题，避免「解锁」这类泛词把整页都召回。
- 新增 `COOLAPK_ALLOWED_HOSTS` / `COOLAPK_ALLOWED_ORIGINS`：配置后开启 FastMCP 的 DNS rebinding 防护并只放行列出的 Host/Origin；不设仍保持关闭，兼容「公网 IP:端口」直连部署。
- streamable-http 新增 `/status` 端点（与 `/mcp` 同一 Bearer 鉴权）：返回版本、运行时长、登录态、限速参数、缓存命中、按 `error_type` 分类的请求/重试指标与调用延迟（`calls` / `latency_ms.total|max|avg`），便于外部监控抓取；`/health` 保持极简不碰磁盘。
- CLI 补齐 5 个与服务端对齐的命令：`app-detail`（应用详情）、`author`（画像 + 专长推断 + 高分动态）、`suggest`（联想词）、`get-url`（只读透传，输出前剔除敏感键）、`image-link`（`/img` 签名外链，需 `COOLAPK_PUBLIC_BASE`）。
- 新增 `COOLAPK_LOG_JSON` / `COOLAPK_LOG_LEVEL`：开启后日志输出 JSON Lines 到 stderr（`ts`/`level`/`logger`/`msg`，异常时多一个 `exc` 只留最后一行），容器与采集器可直接按行解析，stdio 模式不污染协议帧。
- 新增 `.github/workflows/release.yml`：推送 `vX.Y.Z` tag 时校验 tag 与 `pyproject` / `__version__` 一致，构建 sdist/wheel 并附加到 GitHub Release（不推 PyPI）。
- `coolapk_read_image` 支持一次多张图片（逗号/空格/换行分隔，最多 9 张）：九宫格帖此前要调 9 次，现在一次看全；任一张下载失败会指明是第几张，不再静默返回「这帖只有这么多图」。
- `coolapk_feed_detail` 的 `feed_id` 与 CLI `coolapk feed` 现在接受酷安分享链接或整段分享文案（含 `?id=` / `feedId=` 形态），自动解析出帖子 ID；只有酷安域名下的链接参与解析，其他站点链接（如 GitHub issue 页里的数字）与整段文案中的手机号不会被当成 ID，多个不同酷安 ID 并存时明确报错而不是随便挑一个。
- 列表类工具（`coolapk_search` / `coolapk_home` / `coolapk_board` / `coolapk_secondhand` / `coolapk_notify`）统一返回 `has_more` 与 `next_page`，续抓口径与 `coolapk_feed_detail` / `coolapk_user_feeds` 对齐：此前只能盲猜 `page+1`，翻过头拿到空列表也判断不出「到底了」。
- `coolapk_search` 结果为空时新增 `hint`，区分「被关键词严格过滤」（给出被滤条数并建议 `strict=false` 或换词）、「只剩卡片/广告位」与「酷安确实没有」。

### 变更

- `coolapk_home` 返回结构由裸列表改为 `{tab, page, fetched, has_more, next_page, feeds}` 包装：此前 agent 无从判断拿到的是哪一栏、也没有分页信息；未知 `tab` 取值统一按 `recommend` 处理，回显真实分栏并附 `tab_fallback=true` 说明取值被改写。CLI `coolapk home` 同步输出这一包装（此前只打印裸列表，也没有分页信号，且 `--tab 关注` 会被参数校验直接拒绝）。
- 所有时间字段统一为 ISO8601 + 东八区偏移（如 `2023-11-15T06:13+08:00`）：此前用 UTC 且不带时区标记，东八区用户看到的时间比实际早 8 小时，跨日帖还会认错日期。
- `coolapk_feed_detail` / `coolapk_user_feeds` / `coolapk_author_digest` 新增 `truncated` / `next_page`（及 `sub_replies_truncated`）：撞到页数或请求预算上限时明确告知「还有没取的」，此前只能靠条数猜。
- `coolapk_topic` 详情与动态拆分降级：话题详情接口被风控时仍返回动态列表，并在 `topic_error` / `feeds_error` 标注原因；此前任一路失败即整次失败。
- `coolapk_get_url` 严格模式改为默认开启（`COOLAPK_GET_URL_STRICT=1`）：除写端点黑名单外，还要求末段形态像只读端点（`detail`/`list`/`info`/`space`/`search`…）或以纯数字 id 结尾，未来新增的写端点也会被挡住；确需透传非常规只读路径时设 `COOLAPK_GET_URL_STRICT=0`。
- `COOLAPK_ALLOW_ANONYMOUS=1` 现在真正生效：此前它只解除 HTTP 启动拦截，客户端仍会在本地预检里把未登录请求判成 `auth_expired`（匿名只读实际不可用）；现在未登录时按匿名发请求、不带 `X-App-Token`/Cookie。
- `coolapk_home` 的 `tab` 语义修正：`recommend` 改为真推荐流（`/v6/main/indexV8`），关注流独立成 `follow`（同时接受 `dynamic` / `关注`）。此前 `recommend` 实际请求的是 `V9_HOME_TAB_FOLLOW`（关注流），关注少的人会拿到几条甚至空列表，看起来像工具坏了。
- streamable-http 鉴权默认收紧：绑定回环地址且未设 `COOLAPK_HTTP_TOKEN` 时同样拒绝启动（此前只有非回环才拒绝，而 README 推荐的反代拓扑恰好落在回环上，等于公网可达却零鉴权）。确需无鉴权的内网联调必须显式设 `COOLAPK_ALLOW_ANONYMOUS=1`。

### 优化

- 图片外链签名收敛为单一实现（`coolapk_mcp/img_link.py`）：`/img` 端点、`coolapk_image_link` 工具与 CLI `image-link` 共用同一份域名白名单、TTL 夹取与 HMAC 拼接，避免三处各写一遍后的静默 403。
- CI 增 Windows 腿（Python 3.12，覆盖 icacls/路径分支）与 Docker 腿（镜像构建 + 非 root 容器内 CLI 冒烟），Linux 覆盖率改为 `--cov-fail-under=60` 门槛，挡住「新增整块代码零测试」。
- /status 的 `requests` 增加 `calls` 与 `latency_ms`：延迟按逻辑调用统计（含缓存命中与单飞搭车），排障时能直接看出是慢在限速还是上游。
- /status 取 client 或指标失败时降级为 `{"status":"degraded",…}`（HTTP 仍 200）：此前配置损坏会让探针拿到 500，与「进程挂了」无法区分。
- `coolapk_read_image` 的多图返回改用 `@mcp.tool(structured_output=False)` 显式声明非结构化输出：此前靠「`list[Image]` 这种参数化注解会让 FastMCP 降级」这一内部行为隐式成立，MCP 版本一动就可能在 import 期抛 `PydanticSchemaGenerationError` 让整个服务起不来。
- 指标覆盖面补齐：非 `CoolapkError` 的意外异常记为 `internal:<异常类名>`（此前一律零记录），SMS/验证码登录链路的请求也计入 `requests`（此前恒为 0，与真实上游流量不符）；单飞搭车者不再被算成「缓存未命中」，命中率回归真实。
- 长文本不再无上限地塞进上下文：正文超 `COOLAPK_FEED_CONTENT_MAX`（默认 2000 字）、单条评论超 `COOLAPK_REPLY_CONTENT_MAX`（默认 800 字）截断并标 `content_truncated`；单次回复条数超 `COOLAPK_REPLY_MAX_ITEMS`（默认 60）时保留前 N 条并标 `replies_omitted`（`all_replies` 一条帖能翻 300 条）；被本地条数上限裁掉的部分不再给 `next_page`（那是本地裁剪不是上游翻页），要继续往后看请传 `reply_page`；三项设 `0` 均可关闭限制。
- CLI `coolapk feed` 与服务端语义对齐：`--focus` 活跃时不再预拉默认回复（那一页会被整份替换，白排一次限速窗口），聚焦两路都空/失败时回拉一页兜底并标 `comments_focus.fallback`；新增 `--focus-page` 消费 `focus_meta.next_page`；`--grep` 与 MCP 工具一样改为在条数截断之前检索。
- 结构化日志细节：`httpx`/`mcp`/`uvicorn` 固定压到 `WARNING`（它们的 INFO 会把带 token/sessid 的完整请求 URL 写进日志流）；U+2028/U+2029/U+0085 转义保证「一条一行」；只替换自己上次装的 handler，宿主（pytest、外层 uvicorn）已装的 handler 不再被丢弃。

### 修复

- pywebview 网页登录（`coolapk login --web` 回退路径）传参错误：`webview.start(poll, window)` 把窗口对象当成参数序列，pywebview 会以 `poll(*window)` 调用而抛异常，导致该回退通道失败；已改为 `webview.start(poll, (window,))`。
- 浏览器 CDP 登录的调试端口开放范围收窄：`--remote-allow-origins=*` 改为仅允许本机登录页来源（`http://127.0.0.1:<port>` 与 `http://localhost:<port>`），避免登录期间本机任意网页都能连上调试端口。
- `coolapk_get_url` 路径校验漏掉 fragment：`/v6/feed/like#x` 能绕过写端点拦截（httpx 发包时会丢掉 `#` 之后的内容，实际请求的正是写端点），现在按去掉 fragment 的路径判定。
- `/img` 遇到非 ASCII 签名不再 500：`hmac.compare_digest` 对含非 ASCII 的字符串会抛 `TypeError`，任何扫描器都能稳定触发；现在先按 32 位十六进制形态挡掉再比较，统一返回 403。
- `/img` 跳转校验改为「建连前 + 事后」双闸：原先先发起请求才检查 `resp.url.host`，指向内网的 GET 已经发出；现在主机/端口白名单在任何网络动作之前判定，跟随重定向后再复检最终 host。`coolapk_read_image` 同样校验最终 host（响应体会回给模型，属于可读 SSRF）。
- `coolapk_feed_detail` 的关键词检索改为在条数截断之前执行：此前先把回复截到前 60 条再按关键词过滤，命中落在第 61 条之后的评论会被报成「没有」（假阴性，而用户问的正是「有没有人提到 X」）；现在按全部已取回复检索，命中数超过上限时裁到上限并用 `replies_omitted` 与 `comment_search.truncated` 说明。
- 分页中途失败不再丢已取结果：第 3 页报错时前两页会被整体丢弃，还会谎报「没有评论」；现在保留已取条目并标 `truncated` / `next_page`。
- `comments_focus` 单路失败不再谎称取全：楼主/热门任一路失败时 `truncated=true` 并给出 `missing_sources`；`next_page` 取两路较大值，新增 `start_page`（对应 `coolapk_feed_detail` 的 `focus_page`）支持续抓。
- `uid_index.json` 写盘不再丢更新：刷盘前先拍快照再清脏标记，`close()` 无条件保存；此前在途写盘会覆盖 `close()` 前的最后一次更新。
- 登录态刷新不再关掉并发在用的 client：旧实例进 retired 列表、延迟到服务退出统一关闭；已取到旧实例的请求拿到可重试的 `network` 错误，而不是 `internal`。
- 浏览器登录端口竞态：`_free_port()` 释放到浏览器真正绑定之间被抢时，换端口自动重试一次（症状是进程活着但调试口不通），仍失败才报 `timeout`。
- `coolapk login --adb` 失败不再静默返回：输出异常类型与三条排查提示（adb 是否在 PATH、手机是否授权 USB 调试、酷安是否已登录且 root）。
- Windows 凭据文件不再继承目录 ACL：`config.json` 与 `img_secret` 写入后调用 `icacls /inheritance:r /grant:r <当前用户>:F`，只保留当前用户可读写（此前 Windows 分支完全没做权限收紧，同机其他账户可读 token 与完整 Cookie）。
- 弱 `COOLAPK_HTTP_TOKEN` 启动时给警告：少于 16 位、含首尾空白或等于示例值 `coolapk-mcp-dev` 都会在日志里点名（不阻止启动）。这类问题此前只会表现成「客户端莫名 401」。
- 列表类接口的运营卡片不再变成幽灵条目：卡片本身没有帖子 id，此前被当成帖子收下（`id=null`），既撑大 `fetched` 又把 `has_more` 顶成 true；现在卡片只保留内嵌的 `feed` 实体，卡位仍计入本页条数，末页判定不受影响。
- 搜索空结果的「只剩卡片/广告位」提示不再建议改 `strict`：卡片是固定运营位，与严格过滤无关，按旧提示重试只会拿到完全一样的结果；现在直接建议换页码或换个更通用的关键词。
- 搜索分页按真实页大小判定：搜索一页约 20 条，此前沿用列表类的 10 条阈值，真实末页也会标 `has_more=true` 让人白翻一页；现在用 `COOLAPK_SEARCH_PAGE_SIZE`（默认 20）。
- 关键词过滤改用未截断的原始正文匹配：此前拿已经截到 2000 字的 `content` 去比，关键词落在正文后半段时会被误判成不命中。
- 环境变量填 `inf` / `1e400` 不再让进程在 import 期崩溃：`COOLAPK_PAGE_SIZE`、`COOLAPK_SEARCH_PAGE_SIZE`、`COOLAPK_FEED_CONTENT_MAX` 等解析时的 `OverflowError` 现在与其他非法值一样回落默认。
- `coolapk_read_image` 对重复 URL 去重：同一张图粘两遍不再下载两次。
- 分页阈值传 `0` 或负数时不再被抬成 1：旧行为会让 `has_more` 恒为 true，现在直接判定为「已达上限」返回 `has_more=false`。

### 优化

- 图片下载共用连接池：`coolapk_read_image` 与 `/img` 中转不再每个请求新建 `httpx.AsyncClient`（此前每张图一次 TLS 握手与连接池创建），改为共享 Keep-Alive 连接池并在服务退出时统一关闭。
- 图片响应增加 15MB 硬上限：`/img` 与读图都按 `Content-Length` 与累计流长度双重校验，超限返回 413；`/img` 还校验重定向后的最终 host 仍在酷安图片域内，防止签名链接被 302 带到任意地址。
- `/img` 与 `coolapk_image_link` 不再共用硬编码 `coolapk-mcp-dev` 默认密钥（`COOLAPK_IMG_SECRET` 可显式指定，缺省生成随机密钥并持久化到 `~/.coolapk-mcp/img_secret`）。
- Docker 镜像改为非 root 运行（用户 `coolapk`，uid 10001），持久化目录相应改为 `/home/coolapk/.coolapk-mcp`。
- 分页末页判定阈值可配（`COOLAPK_PAGE_SIZE`，默认 10）：此前硬编码「不足 10 条即末页」，接口页大小一变就会提前截断或多翻一页。
- 单页请求纳入时间预算：`Budget.deadline` 此前只约束页与页之间，单页卡在 httpx 超时里仍可超出整个预算；现在每页用剩余时间包一层 `asyncio.wait_for`，超时按截断返回已取部分。
- 配置文件解析结果按 stat 签名缓存：`AppConfig.load()` 此前每次调用都读盘并 `json.loads`（远程 HTTP 下每个工具调用都会走到），现在文件未变即复用解析结果、文件一改立刻失效，热加载行为不变。
- 关键词过滤与 `coolapk_search` 共用同一归一化（去空白 / `#` / `＃` / `_`）：`#解锁#` 这类带话题符号的关键词此前在 `coolapk_user_feeds` / `coolapk_author_digest` 里匹配不到正文，与搜索口径不一致。
- 内部清理：删除从未被引用的 V2 token 算法（`auth/token.py`）与私信模型（`MessageModel` / `ChatSessionModel`），包内 `__version__` 与 `pyproject.toml` 对齐到 0.7.0。
- `X-App-Token` 增加短 TTL 复用（`COOLAPK_TOKEN_TTL`，默认 60 秒）并移出事件循环（`asyncio.to_thread`）：此前每个请求都在事件循环里同步跑一次 bcrypt（约 60ms 纯 CPU），既浪费算力又阻塞其它并发任务。
- `uid_index.json` 改为合并写盘（`COOLAPK_INDEX_SAVE_DELAY`，默认 1 秒）并线程卸载：此前每来一个新 feed_id 就把整份索引（最多 2000 条）同步写一次盘，翻页时等于每页一次阻塞 I/O；退出前由 `close()` 兜底刷盘。
- 限速器支持分桶（`COOLAPK_RATE_LIMIT_MODE=bucket`）：按 `/v6/<资源>` 分桶各自计时，跨桶可并行、桶内仍严格串行；默认 `global` 与历史行为完全一致。
- `coolapk_feed_detail` 的 `comments_focus` 两路评论（楼主 / 热门）改为并发发起：默认全局限速下与串行等价，开启 `bucket` 模式后才真正并行缩短耗时。
- `comments_focus` 活跃时不再预拉一页默认回复（那一页会被聚焦结果整份替换，纯属多排一次限速窗口）；两路都为空时才回拉一页兜底并标 `fallback=default_replies`。
- `coolapk_author_digest` 的动态列表与作者资料改为并发请求（互不依赖），资料页失败只降级为 `author_error`，不再连带失败。
- GET 缓存增加字节上限（`COOLAPK_CACHE_MAX_BYTES`，默认 8 MB）与命中/淘汰统计：此前只按 256 条限制，几十条长评论列表就能占几十 MB。
- 退避等待增加上限（`COOLAPK_MAX_BACKOFF`，默认 30 秒）：此前 `COOLAPK_MAX_RETRIES` 配到 8 时单次等待可超过 100 秒。
- 限速等待抽出可替换出口（`throttle._sleep`），对应测试不再靠墙钟断言，慢 runner 上不会假红。
- `X-App-Token` 生成加单飞：并发首请求只跑一次 bcrypt，其余等同一个 future；异常面放宽到 `Exception` 并统一归 `config` 附带修复路径（此前只有 `RuntimeError`/`OSError` 被兜，`ValueError` 会穿成 `internal`）。
- `device_code` 纳入客户端指纹与缓存键：换机/重装后设备码变了会重建 client，不再一直用旧设备码签 token。
- `/img` 的 `HEAD` 请求不再下载整图：此前探活也会把最多 15MB 正文读完再丢弃，现在上游同样用 `HEAD`，只回状态与类型。
- 凭据目录权限：POSIX 下 `~/.coolapk-mcp` 收紧为 `0700`；`img_secret` 改为先按 `0600` 建文件再写入，消除 `write_text` 沿用 umask 的短暂 `0644` 窗口。
- DNS rebinding 防护可按需开启（`COOLAPK_ALLOWED_HOSTS` / `COOLAPK_ALLOWED_ORIGINS`）：不设仍保持关闭以兼容公网 IP 直连，设置后只放行列出的 Host。
- `/img` 增加并发信号量与 `X-Content-Type-Options: nosniff`；`Cache-Control` 的 max-age 跟随签名剩余有效期（此前固定 86400，可能把图片缓存到签名过期之后），仅接受四种位图类型，svg 等回落到 jpeg；显式非标端口直接 403。
- Bearer 比较改为字节级，重复 `Authorization` 头直接拒绝（反代与进程可能各自取到不同值）；图片签名密钥与 Bearer token 彻底分离（`img_secret` 不再复用 `COOLAPK_HTTP_TOKEN`）。
- 测试体系收口：新增 `/img` 与 Bearer 端点 19 例、CLI 6 例、浏览器登录 5 例；`conftest.py` 默认打桩 `X-App-Token` 并把所有导入期路径常量重定向到临时目录，整套测试不再依赖本机登录态与 `~/.coolapk-mcp`。
- CI 扩到 Python 3.10 / 3.12 / 3.14，新增 `coolapk --help` 冒烟、并发取消与超时、覆盖率输出（只打印不设门槛，当前基线约 56%）。
- Dockerfile 依赖层与代码层分离（改一行代码不再重装整套依赖），HEALTHCHECK 改 exec 形式并放宽 start-period，新增 `.dockerignore` 剔除 tests/docs/临时目录。
- 打包元数据补齐 `readme` / `license` / `authors` / `urls` / `classifiers`（含 3.14），`license-files` 增补 `NOTICE.md`。

## 0.6.9（2026-11-08）

### 修复
- **短信登录**：兼容酷安登录页改版，`requestHash` 解析支持 `createAuthCaptchaFlow('/auth/login', 'hash')` 新格式（老格式仍兼容）；解析失败时的报错附 Issue 反馈链接。
- **ADB 登录**：`adb shell su -c cat` 超时从 10 秒放宽到 30 秒并自动重试一次——修复 adb 冷启动时「未能从手机提取 Cookie」的偶发失败。

## 0.6.8（2026-11-07）

### 修复

- `coolapk login --sms` 在酷安登录页改版后失效：页面把 requestHash 从 `requestHash: 'xxx'` 改成了 `createAuthCaptchaFlow('/auth/login?type=mobile', 'xxx')` 调用参数，老正则零命中导致「无法从登录页解析 requestHash」。现已兼容新老两种格式（实测真实登录页解析成功），解析失败时的报错文案同时引导到仓库 issues。

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
