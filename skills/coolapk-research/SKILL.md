---
name: coolapk-research
description: 用酷安 MCP 工具检索酷安社区资料的使用指南——何时用酷安搜资料（玩机教程、root/解锁 BL、Magisk 模块、ROM/刷机等）、工具调用策略（search → feed_detail → read_image → topic）、分类→话题映射表、风控礼仪。当需要从酷安社区找玩机资料、了解酷友讨论、查机型相关帖子时使用。
---

# 酷安资料检索指南

酷安（CoolAPK）是国内安卓玩机社区的主阵地：写机教程、root/解锁 BL 资源、Magisk 模块、ROM/刷机包、机型适配讨论都集中在这里。本 skill 指导 agent 通过酷安 MCP Server（`coolapk_*` 工具族）检索这些资料。

前置条件：MCP 客户端（dsh / Claude Desktop / Cursor 或其他任意 harness）已按 [README.md](../README.md)「Agent 接入」一节配置酷安 MCP Server（任何 agent 都可照该节自助完成接入），并完成登录态准备（推荐安装第三方酷安桌面端登录一次，见 README「准备清单」）。

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

标准检索链路，按需截断：

```text
coolapk_search        先定位：搜关键词拿帖子列表，选 1–3 个高价值帖
coolapk_feed_detail   读帖：拿正文与回复；需要完整舆情时用 all_replies 或 comment_keyword
coolapk_read_image    看关键截图：教程帖的步骤截图、模块配置截图（image.coolapk.com 直连 567，必须走此工具）
coolapk_topic         刷话题：with_feeds=true 拿话题下最新动态，跟踪持续讨论
```

要点：

- `coolapk_search` 保持默认 `strict=true`（滤掉机型名单广告与不相关结果）；结果太少再试 `strict=false` 或换同义词；
- `coolapk_feed_detail` 内置风控回落（自动从作者动态取全文），无需额外参数；
- 查「某模块翻车了吗」类舆情，用 `comment_keyword` 检索评论而不是通读全部回复，省 token；
- `coolapk_home` 只在需要「最近社区整体在聊什么」时用；
- `coolapk_get_url` 是逃生舱：需要上表工具没覆盖的只读接口时才用，路径以 Coolapk-API-Collect（github.com/XiaoMengXinX/Coolapk-API-Collect 等社区整理） 实测可用清单为准。

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
- **遇到 `error_type: auth_expired`**：不要反复重试受登录态影响的调用，直接引导用户重新登录，按优先级：① 用户已装[酷安第三方桌面端](https://github.com/coolapk-desktop/coolapk-desktop)（酷安官方没有桌面端，这是社区维护的非官方客户端）→ 让用户在桌面端重新登录后直接重试即可，MCP auto 模式自动复用其登录态，无需重启 MCP；② 未装桌面端 → 让用户在仓库目录终端运行 `.venv\Scripts\coolapk login --sms`（短信登录；`--web` 在 Python 3.14 下已知不可用）或 `--cookie` 导入 Cookie；登录完成后再继续；
- **不做写操作**：本 MCP 是纯只读的，任何点赞 / 回复 / 关注 / 发帖请求都应拒绝并说明。

## 输出建议

面向用户汇总检索结果时：注明信息来自酷安哪个帖子（标题 + 作者），教程类内容给「帖子原文步骤 + 截图佐证」，提醒 root / 刷机类操作有变砖风险，步骤以帖子原文与用户机型实际情况为准。
