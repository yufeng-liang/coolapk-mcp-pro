---
name: coolapk-mcp-release
description: "本仓库（coolapk-mcp-pro）发版指南（含 release 说明模板）——当用户说发版/打tag/发release/更新release，或一次功能修复需要发布 GitHub 版本时使用，覆盖 tag 与 Release 发布全流程及发布后的 submodule 指针、服务器同步。"
version: "1.1.0"
license: MIT
---

# GitHub 发版（tag + release 全流程）

> 本文件是 coolapk-mcp-pro 仓库的发版规范，参与贡献请遵守。全局版另存于贡献者本机 `~/.agents/skills/coolapk-mcp-release/`。

用户说「发版」「打 tag」「发 release」时按本文件执行。核心纪律：**发版不只是 push commit——tag 和 release 极易漏打**，历史上 v0.4.0/v0.5.0/v0.6.5 都曾只有代码没有 release，事后补才对齐。

## 何时触发

- 用户明确说：发版 / 打 tag / 发 release / 更新 release / 补 release。
- 隐含信号：版本号已在 CHANGELOG 写了新条目、pyproject 版本已改，但用户说「发布」——必须走完本文全部步骤，不能只 push commit。

## 发布前检查（三处版本一致性）

1. `pyproject.toml` version、包内 `__version__`（`coolapk_mcp/__init__.py` 或对应包）、`CHANGELOG.md` 顶部段三者版本号一致；CHANGELOG 顶部段必须是本次版本。
2. `git status` 干净——未提交的杂项改动先单独提交或隔离，不得混入版本提交。
3. 确认本地与远端都**不存在**目标 tag：`git tag -l vX.Y.Z` + `git ls-remote --tags origin vX.Y.Z`。已存在则禁止覆盖，先提醒用户。

## 发布流程（顺序固定，缺一不可）

```bash
# 1. 提交（中文 commit message：<type>: <描述>）
git add <本次改动文件> && git commit -m "feat: <一句话卖点>"

# 2. 打带注释 tag（注释里带一句版本主题）
git tag -a vX.Y.Z -m "<一句话主题>"

# 3. 推送分支与 tag（一起推）
git push origin <branch> vX.Y.Z

# 4. 双向核对
git rev-parse vX.Y.Z^{commit}          # 解引用出 commit
git ls-remote --tags origin vX.Y.Z     # 远端一致

# 5. 建 release（说明文件先行，见下方模板）
gh release create vX.Y.Z --title "X.Y.Z：<一句话卖点>" --notes-file <notes.md> --latest
```

- 仓库已配置 `.github/workflows/release.yml`：推送 `v*` tag 后自动校验「tag / `pyproject.toml` version / `__version__`」三者一致、`python -m build` 出 sdist+wheel，并创建或更新 Release 附加 `dist/*`（说明由 `generate_release_notes` 按 PR 标题生成）。**因此第 5 步可省**：push tag 后等 Action 变绿，再 `gh release view vX.Y.Z` 核对资产与说明；Action 失败（版本不一致等）时再按上面 `gh release create` 手工兜底，并在 Release 说明里按下方模板补齐三段式。
- `--latest` 给当前最新正式版；补历史 release 时不要加。
- notes 文件放临时目录（`/h/tmp/rel/` 或 `mktemp`），不进仓库。
- 验收：`gh release list` 能看到新 release 且标题/说明正确；`gh release view vX.Y.Z` 说明渲染正常。

## 语言要求：写给不读代码的人看（硬要求，先读这一节）

Release 的读者是**用这套工具的人**（常常还是让 AI 代读的人），不是维护者。他们不知道 `has_more`、`error_type`、`structured_output` 是什么，也不会去翻源码。判断标准只有一条：**一个只会用、没读过一行代码的人，看完能说出「这版解决了我什么麻烦」**。

四条硬要求：

1. **第一行先给一句人话**：`一句话：<这版最大的好处，用生活语言说>`。不写「重构」「收敛」「对齐」「闭环」这类内部词。
2. **每条按「以前怎样 → 现在怎样」写**，先说用户遇到的麻烦（报错原文可以引），再说现在的样子。参数名、环境变量名、命令名可以作为补充放在句中或括号里，**一段话最多出现一个**，其余用中文说清用途。
3. **数字要给参照物**：不写「性能提升 30%」，写「大图能省 70~80% 流量」；不写「排序更优」，写「搜『省电』，默认首条 3 个赞，按热门首条 3289 个赞」。
4. **会让人动手改配置的，单独一段前置**（`## 升级前先看两条`），并写清「不改会怎样」。没影响就明确写「不用改配置，更新即生效」——不许留空让人猜。

反例 → 正例：

- ❌ `新增 _paging_meta，home/board/secondhand 回显 has_more/next_page`
  ✅ `翻页知道还有没有下一页：列表会明确告诉你「下一页是几」，翻到头拿到空列表也不会再以为工具坏了`
- ❌ `get_url 白名单收敛，strict 模式拒绝非常规形态`
  ✅ `命令行那个「透传任意只读接口」的开关收紧了：以前能被 `#` 绕过，现在不行；确实要用老行为就设 `COOLAPK_GET_URL_STRICT=0``
- ❌ `修复 throttle 时间断言 ulp 误差`
  ✅ `一个跟时间有关的自动测试在 Windows 上会误报失败，已修（只影响开发时的自动检查，不影响使用）`

发布前自检（四条全过才算写好）：第一行是「一句话」；没有裸奔的内部术语；该改配置的写清了后果；读一遍能向别人复述这版干了什么。

**这条也管自动生成的说明**：`release.yml` 用 `generate_release_notes` 按 PR 标题生成的正文是给人看不出所以然的，发布后必须用下方模板重写一遍（`gh release edit vX.Y.Z --notes-file <notes.md>`）；历史 release 同样按本规范维护，逐个 edit 后全量核对。

## Release 说明模板（口语化四段式，逐字遵守）

```markdown
## 这版做了什么

一句话：<这版最大的好处，生活语言>

- **<以前怎样>**：<用户遇到的麻烦，可引报错原文>；现在<变成什么样>。
- **<以前怎样>**：…
（3~6 条；同一功能的实现调整与修正合并成一条，新增功能开发期修的问题也归进该功能）

## 升级前先看两条        ← 没有就不写这一段

- **<要改什么>**（`ENV_NAME`）：不改会<后果>。
- **<行为变化>**：<怎么恢复老行为>（`ENV_NAME=0`）。

## 怎么更新

- **装在本机的**：`pip install -U git+https://github.com/yufeng-liang/coolapk-mcp-pro.git`，然后重启 MCP 客户端即可。
- **装在服务器的**：`git pull && pip install -q . && systemctl restart coolapk-mcp`。
- 配置变化：<新增环境变量/默认值变化>；没有就写「不用改配置，更新就生效」。

完整变更见 [CHANGELOG](https://github.com/yufeng-liang/coolapk-mcp-pro/blob/main/CHANGELOG.md)。
```

标题格式：`X.Y.Z：<一句话卖点>`（例：`0.6.7：读图缩略优先，省 70~80% 流量`）。标题同样用生活语言，不写术语。

写说明的其余规则：
- 面向用户可感知的变化归纳，同一功能在版本内的实现调整与修正合并成一条。
- 新增功能开发期间修正的问题归入该新增功能，不算修复上一版 bug。
- 构建通过/内部重构/未验证的效果不写。
- 基于实际 diff 核对，不把 commit 标题逐条罗列。

## 更新已有 release（补说明/改标题）

```bash
gh release edit vX.Y.Z --repo <owner/repo> --title "新标题" --notes-file <notes.md>
```

补历史 release 时逐个 edit，最后 `gh release list` 全量核对。

## 仓库附带义务（coolapk-mcp-pro 项目专属）

发布仓推送后还有两步，容易漏：

1. **主仓 submodule 指针**：`H:\coolapk-auto-post` 里 `mcp-server/` 是 submodule，`git add mcp-server && git commit -m "chore: mcp-server submodule 指针更新至 coolapk-mcp-pro <hash>"` 并推送。
2. **阿里云服务器同步**：git 协议常挂起，用 codeload 直链兜底——
   `curl -sL https://codeload.github.com/yufeng-liang/coolapk-mcp-pro/tar.gz/refs/heads/main | tar xzf - --strip-components=1 -C /opt/coolapk-mcp/repo`
   然后 `pip install -q --no-cache-dir .`（必须在 repo 目录跑）+ `systemctl restart coolapk-mcp` + `curl /health` 验证。
   对象已进库时可用 `git update-ref` 补 refs 对齐（见项目 engram 记忆）。

## 常见坑

- **tag 漏打**：发版流程固定为 commit → tag -a → push → gh release create，四步一口气做完，中断就要回头检查。
- **release 建了没说明**：`gh release create vX.Y.Z` 不带 `--notes` 会生成空说明，必须 `--notes-file`。
- **误提交临时文件后重打 tag**：`git tag -d vX.Y.Z && git push origin :refs/tags/vX.Y.Z` 删远端 tag 后重打（仅限刚发布、无消费者时）。
- **最新版判断**：正式发新版必须 `--latest`，否则 GitHub 按时间排序可能把旧版标成 Latest。
