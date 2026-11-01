---
name: coolapk-research
description: 用酷安 MCP 工具检索酷安社区资料的使用指南——何时用酷安搜资料（玩机教程、root/解锁 BL、Magisk 模块、ROM/刷机等）、工具调用策略（search → feed_detail → read_image → topic）、语义搜索策略链、作者质量锚定、分类→话题映射表、风控礼仪。当需要从酷安社区找玩机资料、了解酷友讨论、查机型相关帖子时使用。
---

# 酷安资料检索指南

酷安（CoolAPK）是国内安卓玩机社区的主阵地：写机教程、root/解锁 BL 资源、Magisk 模块、ROM/刷机包、机型适配讨论都集中在这里。本 skill 指导 agent 通过酷安 MCP Server（`coolapk_*` 工具族）检索这些资料。

前置条件：MCP 客户端（dsh / Claude Desktop / Cursor 或其他任意 harness）已按 [README.md](../../README.md)「Agent 接入」一节配置酷安 MCP Server（任何 agent 都可照该节自助完成接入），并完成登录态准备（推荐安装第三方酷安桌面端登录一次，见 README「准备清单」）。

## 何时用酷安搜资料

适合交给酷安检索的问题：

- **玩机教程**：某机型怎么解锁 BL、怎么刷入第三方 Recovery、怎么过 SafetyNet/Play Integrity；
- **root / 解锁 BL 资源**：某机型的解锁工具、内核漏洞、boot 备份与回滚经验；
- **Magisk 模块**：某功能（如息屏充电加速、去广告、音效增强）有哪些模块、兼容性反馈、翻车报告；
- **ROM / 刷机**：某机型的第三方 ROM（类原生 / HyperOS 移植 / PixelOS 等）口碑、bug 清单、刷机步骤；
- **机型口碑与讨论**：某机型发热、续航、相机表现的真实用户反馈；
- **酷安生态本身**：某话题下大家最近在聊什么、某用户的发帖。

不适合酷安的：官方文档 / API 用法（用 context7 / 官方源）、一般新闻（用 web 搜索）、酷安以外的社区资源。

## 工具调用策略

**语义搜索策略（重要）**：酷安搜索只认关键词，不认自然语言长句。用户需求是
「手机变卡怎么办」「想要更省电」这类口语时，按下面的策略链走：

1. **拆关键词**：把需求语义拆成 1–2 个酷安社区常用词（"手机变卡" → `卡顿 优化`、"更省电" → `省电 续航`）。
2. **话题优先**：先 `coolapk_search`（type=feedTopic）搜话题——酷安话题是最准的语义匹配器（"省电" 直接命中「省电」「MIUI省电」话题），看 follow_num 判断活跃度。
3. **联想词改写**：`coolapk_suggest` 拿酷安建议的规范搜索词，修正生造词。
4. **进话题抓干货**：`coolapk_topic`（with_feeds=true）已默认按热度排序（like+reply*3），前排是高价值帖。
5. **全站搜帖**：`coolapk_search`（type=feed）默认 sort=hot，直接返回高赞干货帖而非低互动点评；结果带 like_num/reply_num，可再按热度筛选。
6. **读帖收图**：`coolapk_feed_detail` 读正文与回复（需要完整舆情时用 all_replies 或 comment_keyword）；关键截图用 `coolapk_read_image` 看。

标准检索链路（按需截断）：

```text
coolapk_search        先定位（默认 sort=hot 高赞优先；type=feedTopic 找话题）
coolapk_suggest       联想词改写：口语 → 酷安习惯用词
coolapk_topic         刷话题：with_feeds=true 默认热度排序，前排即干货
coolapk_feed_detail   读帖：拿正文与回复；需要完整舆情时用 all_replies 或 comment_keyword
coolapk_read_image    看关键截图：教程帖的步骤截图、模块配置截图（image.coolapk.com 直连 567，必须走此工具）
coolapk_image_link    客户端显示不了 base64 图片时（如手机语音助手），把图片转成签名 https 外链；仅远程 HTTP 模式可用
```

要点：

- `coolapk_search` 保持默认 `strict=true`（滤掉机型名单广告与不相关结果）；结果太少再试 `strict=false` 或换同义词；
- `coolapk_search` 默认 `sort=hot`（实测高赞干货帖优先，默认综合排序多为低互动点评）；要最新动态才用 `sort=default`；
- `coolapk_feed_detail` 内置风控回落（自动从作者动态取全文），无需额外参数；
- 查「某模块翻车了吗」类舆情，用 `comment_keyword` 检索评论而不是通读全部回复，省 token；
- `coolapk_home` 只在需要「最近社区整体在聊什么」时用；
- `coolapk_get_url` 是逃生舱：需要上表工具没覆盖的只读接口时才用，路径以 Coolapk-API-Collect（github.com/XiaoMengXinX/Coolapk-API-Collect 等社区整理） 实测可用清单为准。
- 结果里的 `post_score`（点赞+评论×3+转发×2）与 `discussion_ratio`（评论/点赞）是现成的质量分：排序、筛「干货还是凑数」直接用它，不用自己算。

## 作者质量锚定（判断「这帖可信吗」）

**核心事实：帖子火 ≠ 作者权威。** 实测样本里有过单帖 3100 赞但只发过 19 帖、5 级的普通用户；也有 79,166 粉、233 帖、常年更新解锁工具的领域顶流。只看点赞数会把一次性爆款当成教程来源。

判断一个帖子值不值得照做，按这个顺序：

1. **先看作者等级与粉丝**：搜索结果条目里带作者 uid，用 `coolapk_author` 拿画像：
   - `author_tier=top`（≥3 万粉且 ≥10 级）→ 领域顶流，教程可信度高；
   - `author_tier=expert`（≥1 万粉且 ≥8 级）→ 该领域大佬；
   - `author_tier=active`（≥1000 粉且 ≥5 级）→ 活跃创作者，可参考；
   - `author_tier=normal` → 普通用户，翻车风险高，需要交叉验证。
2. **看 `fans_per_feed`**（粉丝/帖数）：数值高说明「精发」，内容密度大；数值低但粉丝多，可能是搬运或水帖多。
3. **看 `expertise`**：作者专长领域列表，与你要找的主题对不上就别当权威。
4. **确认是同一主题的老手**：`coolapk_author_digest`（uid + keyword）一次抓 2 页并按热度排序，能看出他是否在这个主题上持续输出（例如「莫离然然」的解锁工具从 v1.4 迭代到 v2.2，还有解锁节 1.0/2.0 教程）。

**典型用法（用户问「怎么解锁 BL」）**：

```text
1. coolapk_search(type=feedTopic, keyword=解锁BL)    找话题，看哪个话题活跃
2. coolapk_topic(tag=小米解锁BL, with_feeds=true)     热度序拿前排帖
3. coolapk_author(uid=<高赞帖作者>)                   确认是不是大佬
4. coolapk_author_digest(uid, keyword=解锁, pages=2)  拉他全套教程（工具版本链）
5. coolapk_feed_detail(feed_id=...)                  读最新那篇的正文与踩坑回复
```

注意：`coolapk_author_digest` 会抓多页（默认 2 页约 40 条），是按需深挖的工具，不要对每个作者都调用；只有在前 3 步已经判断「这人是大佬」时才用。

**召回瓶颈的真相**：全站搜帖（type=feed）对热门话题的召回并不完整——实测搜「小米解锁BL」只回 6 条、点赞最高的那条 6907 赞的帖子根本不在结果里，而话题抓帖（`coolapk_topic` with_feeds）第一条就是它。所以找热门主题时**优先走话题通道**，搜帖只用来发现长尾关键词。

## 分类 → 话题映射表

> 设计共识：映射表维护在本 skill 而非代码。话题名以酷安实际存在且可挂载的话题为准；部分话题存在但 id=0 且无热度（如「玩机」「系统」），检索不到内容时换同义词。
>
> **按需扩充**：下表是起始建议，首次使用某分类前可先用 `coolapk_search`（type=feedTopic）确认话题存在与活跃度，再补充进本表。

### 写机教程类

| 话题关键词 | 说明 |
| - | - |
| 玩机技巧 | 泛玩机教程主话题（hot ≈ 2.3 万，follow ≈ 5.6 万，实测 2026-10） |
| 教程 | 泛教程 |
| 机型名 + 教程 | 如「小米15 教程」，先搜机型名再从结果中确认话题名 |

### root / 解锁 BL 类

| 话题关键词 | 说明 |
| - | - |
| Root | root 主话题（hot ≈ 13 万 / follow ≈ 4.4 万） |
| 解锁BL / BL解锁 | 解锁 Bootloader；两个写法都搜一遍 |
| KernelSU | KSU 内核级 root 生态 |
| SukiSUUltra | SukiSU Ultra（hot ≈ 7 万 / follow ≈ 2.2 万），LKM 方案活跃区 |
| APatch | APatch 方案 |

### Magisk 模块类

| 话题关键词 | 说明 |
| - | - |
| Magisk模块 | 模块主话题（hot ≈ 40 万 / follow ≈ 23 万，垂直类里流量最高） |
| Magisk | Magisk 框架本体讨论 |
| 模块推荐 | 模块汇总帖常见话题 |

### ROM / 刷机类

| 话题关键词 | 说明 |
| - | - |
| 刷机 | 刷机主话题（hot ≈ 4 万 / follow ≈ 4.4 万） |
| ROM | 第三方 ROM 讨论 |
| 类原生 | PixelOS / LineageOS 等类原生适配 |
| HyperOS | 小米 HyperOS 官方 / 移植讨论 |
| 机型名 + 刷机包 | 如「红米K70 刷机包」，按机型定位适配帖 |

通用补充：泛流量话题「酷安夜话」（hot ≈ 180 万）适合了解社区整体氛围，与玩机垂直检索区分开。

## 风控礼仪

酷安接口有风控（验证码拦截），MCP Server 已内置限速 / 缓存 / 回落，但 agent 行为仍需克制：

- **不要高频连续调用**：一次任务里工具调用控制在个位数；同一关键词换不同词而不是连续翻很多页；列表页翻页 ≤3 页一般够用；
- **优先命中缓存与已有信息**：同一话题/帖子在一次会话内不要重复拉详情；
- **遇到 `error_type: captcha`**：不要立刻重试同一调用。换信息源（改用话题动态或搜索其它关键词），仍不行就告知用户稍后再试；
- **遇到 `error_type: auth_expired`**：不要反复重试受登录态影响的调用，直接引导用户重新登录，按优先级：① 用户已装[酷安第三方桌面端](https://github.com/coolapk-desktop/coolapk-desktop)（酷安官方没有桌面端，这是社区维护的非官方客户端）→ 让用户在桌面端重新登录后直接重试即可（服务器会在下次调用时自动重建连接）；若仍失效，重启 / 重载 MCP 会话兜底；② 未装桌面端 → 让用户在仓库目录终端运行 `.venv\Scripts\coolapk login --sms`（短信登录；网页登录 `--web` 推荐 Python 3.12，3.14 下运行时初始化失败，3.14 用户改用 `--sms` / `--cookie` / `--adb`）或 `--cookie` 导入 Cookie；登录完成后再继续；
- **不做写操作**：本 MCP 是纯只读的，任何点赞 / 回复 / 关注 / 发帖请求都应拒绝并说明。

## 输出建议

面向用户汇总检索结果时：注明信息来自酷安哪个帖子（标题 + 作者），教程类内容给「帖子原文步骤 + 截图佐证」，提醒 root / 刷机类操作有变砖风险，步骤以帖子原文与用户机型实际情况为准。
