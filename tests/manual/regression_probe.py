# -*- coding: utf-8 -*-
"""审查修复回归探针（离线 mock，零真实网络）

覆盖本轮 5 份审查报告修复项的关键行为：
  B1 顶层 list 响应直通      B2 status=1004/-415 风控分支+不写缓存
  A1 topic tag quote        A3 get_url 双重编码绕过拒绝
  A7 黑名单段匹配不误伤 replyList/message-like   C2 _check_local_auth 空格变体
  C1 _load_desktop_login 非法 JSON 结构不崩   D3 MAX_RETRIES 钳位
  E2 AUTH_EXPIRED_MESSAGE 命令可执行性（静态检查）
  S1 scoring 质量分/作者分级/专长推断    S2 作者白名单与 user_feeds 包装结构
  ui1-ui6 用户可感知改进：首页分栏语义、列表分页信号、搜索空结果解释、
          长文本上限、多图读取、分享链接解析
"""
import asyncio
import contextlib
import importlib
import json
import logging
import os
import os as _os
import sys
from pathlib import Path

# 手动探针：从 tests/manual/ 上溯两级到仓库根，保证源码可导入（也可用已安装包）
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx

import coolapk_mcp.server as _srvmod
from coolapk_mcp.client import AUTH_EXPIRED_MESSAGE, CoolapkClient, CoolapkError
from coolapk_mcp.config import AppConfig
from coolapk_mcp.server import mcp as _mcp_inst

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(f"{name}" + (f" | {detail}" if detail and not cond else ""))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f" | {detail}" if detail and not cond else ""))


def make_client(login: bool = True, desktop_cookie: str = "") -> CoolapkClient:
    cfg = AppConfig()
    cfg.uid = "2781460" if login else ""
    cfg.username = "tester" if login else ""
    cfg.token = "tok" if login else ""
    cfg.desktop_cookie = desktop_cookie
    c = CoolapkClient(cfg)
    # 关掉限速睡眠，加速探针
    class _NoWait:
        # 限速器现在接受桶名（bucket 模式），假实现要跟着签名
        async def acquire(self, bucket: str = ""):
            return None
    c._limiter = _NoWait()
    return c


def patch_transport(client: CoolapkClient, handler) -> None:
    """把内部 AsyncClient 的 transport 换成 mock，handler(request)->httpx.Response"""
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), timeout=30.0
    )


# ---- B1: 顶层 list 响应直通（不再 AttributeError） ----
def probe_b1():
    async def run():
        c = make_client()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[{"id": 1, "title": "x"}])

        patch_transport(c, handler)
        data = await c.get("/v6/search", params={"type": "all", "keyword": "test"})
        check("B1 顶层 list 直通", isinstance(data, list) and data[0]["id"] == 1)
        await c._client.aclose()

    asyncio.run(run())


# ---- B2: status=1004 风控响应抛错且不写缓存 ----
def probe_b2():
    async def run():
        c = make_client()
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, json={"status": 1004, "message": "网络环境异常", "data": None})

        patch_transport(c, handler)
        raised = False
        try:
            await c.get("/v6/main/indexV8")
        except CoolapkError as e:
            raised = e.error_code == 1004 and "风控" in e.message
        check("B2a status=1004 抛 CoolapkError(1004)", raised)
        # 再请求一次应走到传输层（未缓存投毒）
        try:
            await c.get("/v6/main/indexV8")
        except CoolapkError:
            pass
        check("B2b 风控响应未写缓存", calls["n"] == 2, f"calls={calls['n']}")

        # -415 分支
        def handler415(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": -415, "message": "设备异常", "data": None})

        patch_transport(c, handler415)
        got415 = False
        try:
            await c.get("/v6/main/indexV8")
        except CoolapkError as e:
            got415 = e.error_code == -415 and "拉黑" in e.message
        check("B2c status=-415 拉黑分支", got415)
        await c._client.aclose()

    asyncio.run(run())


# ---- B2 补: 正常 data 响应仍写缓存 ----
def probe_b2_ok():
    async def run():
        c = make_client()
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, json={"status": 1, "data": {"ok": True}, "message": ""})

        patch_transport(c, handler)
        d1 = await c.get("/v6/main/indexV8")
        d2 = await c.get("/v6/main/indexV8")
        check("B2d 正常响应写缓存", d1 == {"ok": True} and d2 == {"ok": True} and calls["n"] == 1)
        await c._client.aclose()

    asyncio.run(run())


# ---- A1: topic tag 编码（#小米15# 完整进 query，& 注入被转义） ----
def probe_a1():
    async def run():
        c = make_client()
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200, json=[])

        patch_transport(c, handler)
        await c.get_topic_feeds("#小米15#")
        u = seen["url"]
        check("A1a #话题# 完整进 query", "tag=%23%E5%B0%8F%E7%B1%B315%23" in u, u)
        await c.get_topic_feeds("a&uid=1")
        u2 = seen["url"]
        check("A1b & 注入被转义", "a%26uid%3D1" in u2 and "uid=1" not in u2.split("tag=")[1].split("&")[0], u2)
        await c._client.aclose()

    asyncio.run(run())


# ---- A3/A7: get_url 编码绕过与黑名单段匹配 ----
def probe_get_url():
    async def run():
        c = make_client()
        # 双重编码绕过必须被拒
        blocked, allowed = [], []
        for p in [
            "/v6/feed/digg",            # 写端点直写 → 拒
            "/v6/feed/%64igg",          # 单编码 → 拒
            "/v6/feed/%2564igg",        # 双重编码 → 拒
            "/v6/reply/add",            # 写端点 → 拒
            "/v6/message/sendMessage",  # 写端点 → 拒
            "/v6/feed/replyList?id=1",  # 只读 → 放
            "/v6/message-like?uid=1",   # 只读 → 放
            "/v6/apk/detail?id=com.x",  # 只读 → 放
            "/v6/feed/detail?id=1",     # 只读 → 放
        ]:
            try:
                CoolapkClient._check_get_url_path(p)
                allowed.append(p)
            except CoolapkError:
                blocked.append(p)
        check("A3 双重编码 digg 被拒", "/v6/feed/%2564igg" in blocked)
        check("A7a replyList 放行", "/v6/feed/replyList?id=1" in allowed, f"allowed={allowed}")
        check("A7b message-like 放行", "/v6/message-like?uid=1" in allowed, f"allowed={allowed}")
        check("A7c 写端点全拒",
              all(p in blocked for p in ["/v6/feed/digg", "/v6/feed/%64igg", "/v6/reply/add", "/v6/message/sendMessage"]),
              f"blocked={blocked}")
        check("A7d 只读 apk/detail 不被 query 误伤", "/v6/apk/detail?id=com.x" in allowed)
        await c._client.aclose()

    asyncio.run(run())


# ---- C2: _check_local_auth "; SESSID=deleted"（分号后空格）变体 ----
def probe_c2():
    # 用真实三种形态逐个测
    variants = [
        ("SESSID 分号紧贴", "uid=1; username=a; token=t;SESSID=deleted", True),
        ("SESSID 分号空格", "uid=1; username=a; token=t; SESSID=deleted", True),
        ("SESSID 有效", "uid=1; username=a; token=t; SESSID=abcdef123456", False),
        ("无 SESSID 的三键串", "uid=1; username=a; token=t", False),
    ]
    for label, cookie, expect in variants:
        c = make_client(desktop_cookie=cookie)
        try:
            c._check_local_auth()
            got = False
        except CoolapkError as e:
            got = e.kind == "auth_expired"
        check(f"C2 {label} → {'expired' if expect else 'ok'}", got == expect)



# ---- C1: _load_desktop_login 非法结构不崩 ----
def probe_c1():
    import tempfile

    import coolapk_mcp.config as cfgmod

    orig = cfgmod.DESKTOP_ACCOUNTS_FILE
    tmpdir = tempfile.mkdtemp()
    fake = Path(tmpdir) / "accounts.json"
    cfgmod.DESKTOP_ACCOUNTS_FILE = fake
    try:
        for payload, label in [
            ("null", "顶层 null"),
            ("[1,2]", "顶层 list"),
            ('"str"', "顶层 str"),
            ('{"accounts": ["x", 3]}', "accounts 元素非 dict"),
            ('{"lastLoginUid": 1, "accounts": [{"uid": 1, "cookie": "a=1; SESSID=x"}]}', "cookie 缺 token"),
        ]:
            fake.write_text(payload, encoding="utf-8")
            try:
                r = cfgmod._load_desktop_login()
                check(f"C1 {label} 不崩(返回 None)", r is None)
            except AttributeError as e:
                check(f"C1 {label} 不崩(返回 None)", False, f"AttributeError: {e}")
        # 合法结构仍能读出
        fake.write_text(json.dumps({
            "lastLoginUid": 42,
            "accounts": [{"uid": 42, "username": "u", "cookie": "uid=42; username=u; token=tk; SESSID=abc"}],
        }), encoding="utf-8")
        r = cfgmod._load_desktop_login()
        check("C1 合法结构读出", r is not None and r["uid"] == "42")
    finally:
        cfgmod.DESKTOP_ACCOUNTS_FILE = orig


# ---- D3: MAX_RETRIES 钳位 ----
def probe_d3():
    import importlib
    import coolapk_mcp.throttle as th

    saved = os.environ.pop("COOLAPK_MAX_RETRIES", None)
    try:
        os.environ["COOLAPK_MAX_RETRIES"] = "-3"
        importlib.reload(th)
        check("D3a 负值钳位到 0", th.MAX_RETRIES == 0, f"got {th.MAX_RETRIES}")
        os.environ["COOLAPK_MAX_RETRIES"] = "1.7"
        importlib.reload(th)
        check("D3b 浮点截断=1", th.MAX_RETRIES == 1, f"got {th.MAX_RETRIES}")
        os.environ["COOLAPK_MAX_RETRIES"] = "5"
        importlib.reload(th)
        check("D3c 正常值透传", th.MAX_RETRIES == 5, f"got {th.MAX_RETRIES}")
        os.environ["COOLAPK_MAX_RETRIES"] = "abc"
        importlib.reload(th)
        check("D3d 非法值回落 2", th.MAX_RETRIES == 2, f"got {th.MAX_RETRIES}")
    finally:
        if saved is not None:
            os.environ["COOLAPK_MAX_RETRIES"] = saved
        else:
            os.environ.pop("COOLAPK_MAX_RETRIES", None)
        importlib.reload(th)


# ---- E2: 提示文案里的命令真实存在（静态检查 pyproject 入口） ----
def probe_e2():
    # 探针位于 tests/manual/，仓库根在上两级
    pyproject = (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    check("E2a 提示用 coolapk 而非 py -m", "py -m coolapk_mcp" not in AUTH_EXPIRED_MESSAGE)
    check("E2b pyproject 注册 coolapk 入口", "coolapk = " in pyproject or "coolapk=" in pyproject)


# ---- 附加: _strip_sensitive 子串匹配（A3 config + 变体键） ----
def probe_strip():
    from coolapk_mcp.models import _strip_sensitive
    sample = {
        "config": {"a": 1},
        "accessToken": "x",
        "userToken": "y",
        "mySecretValue": "z",
        "sessionId": "s",
        "ok": "keep",
        "nested": {"token": "t", "deep": {"secretKey": "v"}},
    }
    out = _strip_sensitive(sample)
    check("S1 config 整键剔除", "config" not in out)
    check("S2 token 变体剔除", all(k not in out for k in ("accessToken", "userToken", "sessionId")))
    check("S3 secret 子串剔除", "mySecretValue" not in out)
    check("S4 嵌套剔除", "token" not in out.get("nested", {}) and "secretKey" not in out["nested"]["deep"])
    check("S5 正常键保留", out.get("ok") == "keep")


# ====================================================================
# ---- v0.3.0 HTTP 传输层：_build_http_app / _bearer_endpoint / /health ----
# 全程离线：ASGI 直驱（httpx ASGITransport + 手工 scope），零 socket 监听；
# 不经 lifespan，绕开 mcp.session_manager，只测路由与鉴权壳。
# ====================================================================
# /health 用例会走 httpx 真实请求管线（ASGITransport），压掉 INFO 请求日志保持输出干净
logging.getLogger("httpx").setLevel(logging.WARNING)


@contextlib.contextmanager
def _http_env(**kv):
    """临时设置 COOLAPK_HTTP_* 环境变量，用例结束后恢复原值；传 None 表示清除该变量。"""
    keys = (
        "COOLAPK_HTTP_HOST",
        "COOLAPK_HTTP_PORT",
        "COOLAPK_HTTP_TOKEN",
        "COOLAPK_ALLOW_ANONYMOUS",
    )
    saved = {k: _os.environ.get(k) for k in keys}
    try:
        for k, v in kv.items():
            if v is None:
                _os.environ.pop(k, None)
            else:
                _os.environ[k] = v
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                _os.environ.pop(k, None)
            else:
                _os.environ[k] = v


def _build_bearer_endpoint(token: str, stub_scopes: list):
    """摘出真实 _bearer_endpoint 闭包：给 mcp.streamable_http_app 打桩返回 stub inner
    （_bearer_endpoint 是 _build_http_app 内嵌函数，无法直接 import），再调
    _build_http_app 从 Mount("/") 上取包好的端点。stub_scopes 收集透传给 inner 的 scope。
    """
    from starlette.routing import Mount

    async def stub_inner(scope, receive, send):
        stub_scopes.append(scope)

    _mcp_inst.streamable_http_app = lambda: stub_inner
    try:
        with _http_env(
            COOLAPK_HTTP_HOST=None,
            COOLAPK_HTTP_PORT=None,
            COOLAPK_HTTP_TOKEN=token,
            # 空 token 现在只有显式开匿名才能构建（H1 已把回环免鉴权取消）
            COOLAPK_ALLOW_ANONYMOUS=None if token else "1",
        ):
            app, _host, _port = _srvmod._build_http_app()
    finally:
        _mcp_inst.__dict__.pop("streamable_http_app", None)
    return next(r.app for r in app.routes if isinstance(r, Mount))


async def _drive_asgi(endpoint, scope):
    """手工 ASGI 三参驱动：自构造 receive，send 收集 http.response.start 与 body。"""
    out = {"start": None, "body": b""}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            out["start"] = message
        elif message["type"] == "http.response.body":
            out["body"] += message.get("body", b"")

    await endpoint(scope, receive, send)
    return out


# ---- H1: 回环 host 无 token → 同样拒绝启动（回环不再是免鉴权的理由：
# 本机浏览器 + DNS rebinding 一样能打到 127.0.0.1） ----
def probe_h1():
    with _http_env(
        COOLAPK_HTTP_HOST=None,
        COOLAPK_HTTP_PORT=None,
        COOLAPK_HTTP_TOKEN=None,
        COOLAPK_ALLOW_ANONYMOUS=None,
    ):
        try:
            _srvmod._build_http_app()
            check("H1a 回环+无token 拒绝启动", False, "未抛 SystemExit")
        except SystemExit as e:
            check("H1a 回环+无token 拒绝启动", "拒绝启动" in str(e), f"文案: {str(e)[:120]}")
            check(
                "H1b 文案指向 COOLAPK_ALLOW_ANONYMOUS",
                "COOLAPK_ALLOW_ANONYMOUS" in str(e),
                f"文案: {str(e)[:120]}",
            )


# ---- H6: 逃生舱 COOLAPK_ALLOW_ANONYMOUS=1 → 无 token 也能构建，port 默认 8000 ----
def probe_h6():
    with _http_env(
        COOLAPK_HTTP_HOST=None,
        COOLAPK_HTTP_PORT=None,
        COOLAPK_HTTP_TOKEN=None,
        COOLAPK_ALLOW_ANONYMOUS="1",
    ):
        try:
            app, host, port = _srvmod._build_http_app()
            check("H6a 匿名开关放行构建", callable(app) and host == "127.0.0.1", f"host={host}")
            check("H6b port 默认 8000", port == 8000, f"port={port}")
        except SystemExit as e:
            check("H6a 匿名开关放行构建", False, f"意外 SystemExit: {e}")
            check("H6b port 默认 8000", False, "构建失败")


# ---- H2: 非回环 host 无 token → SystemExit 且文案含「拒绝启动」 ----
def probe_h2():
    with _http_env(COOLAPK_HTTP_HOST="0.0.0.0", COOLAPK_HTTP_PORT=None, COOLAPK_HTTP_TOKEN=None):
        try:
            _srvmod._build_http_app()
            check("H2 非回环无token 拒绝启动", False, "未抛 SystemExit")
        except SystemExit as e:
            check("H2 非回环无token 拒绝启动", "拒绝启动" in str(e), f"文案: {str(e)[:100]}")


# ---- H3: 非回环 host + token → 正常构建 ----
def probe_h3():
    with _http_env(COOLAPK_HTTP_HOST="0.0.0.0", COOLAPK_HTTP_PORT="9911", COOLAPK_HTTP_TOKEN="probe-token"):
        try:
            app, host, port = _srvmod._build_http_app()
            check("H3 非回环+token 正常构建",
                  app is not None and host == "0.0.0.0" and port == 9911, f"host={host} port={port}")
        except SystemExit as e:
            check("H3 非回环+token 正常构建", False, f"意外 SystemExit: {e}")


# ---- H4: /health 免鉴权 200（不依赖 lifespan，ASGITransport 直驱 Starlette app） ----
def probe_h4():
    async def run():
        with _http_env(COOLAPK_HTTP_HOST=None, COOLAPK_HTTP_PORT=None, COOLAPK_HTTP_TOKEN="probe-token"):
            app, _host, _port = _srvmod._build_http_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as c:
            # token 已设但请求不带 authorization 头仍 200 → 证明 /health 在鉴权壳之外
            r = await c.get("/health")
        check("H4a /health 免鉴权 200", r.status_code == 200, f"got {r.status_code}")
        check("H4b /health JSON {status: ok}", r.json() == {"status": "ok"}, str(r.json()))

    asyncio.run(run())


# ---- H5: _bearer_endpoint 直测（手工 ASGI scope + stub inner + send 收集器） ----
def probe_h5():
    async def run():
        TOKEN = "probe-token"
        base_scope = {"type": "http", "method": "POST", "path": "/mcp", "query_string": b""}

        # a. token 已设、scope 无 authorization 头 → 401 + WWW-Authenticate: Bearer
        scopes = []
        ep = _build_bearer_endpoint(TOKEN, scopes)
        ev = await _drive_asgi(ep, {**base_scope, "headers": []})
        start = ev["start"]
        hdrs = dict(start["headers"]) if start else {}
        check("H5a 无auth头 → 401",
              start is not None and start["status"] == 401, str(start and start.get("status")))
        check("H5a2 401 响应头 WWW-Authenticate: Bearer",
              hdrs.get(b"www-authenticate") == b"Bearer", str(hdrs))
        check("H5a3 401 body 含 unauthorized", b"unauthorized" in ev["body"])
        check("H5a4 拒绝时 inner 未被调用", len(scopes) == 0, f"calls={len(scopes)}")

        # b. token 已设、authorization="Bearer wrong" → 401
        scopes.clear()
        ev = await _drive_asgi(ep, {**base_scope, "headers": [(b"authorization", b"Bearer wrong")]})
        check("H5b 错误token → 401",
              ev["start"] is not None and ev["start"]["status"] == 401,
              str(ev["start"] and ev["start"].get("status")))
        check("H5b2 拒绝时 inner 未被调用", len(scopes) == 0, f"calls={len(scopes)}")

        # c. token 已设、authorization 正确 → stub inner 被调用且收到原 scope
        scopes.clear()
        good_scope = {**base_scope, "headers": [(b"authorization", f"Bearer {TOKEN}".encode())]}
        ev = await _drive_asgi(ep, good_scope)
        check("H5c 正确token 放行 inner", len(scopes) == 1, f"calls={len(scopes)}")
        check("H5c2 inner 收到原 scope（无改写）", bool(scopes) and scopes[0] is good_scope)
        check("H5c3 请求原样透传（stub 未回包，无 401 响应产生）", ev["start"] is None)

        # d. token 为空串 → _bearer_endpoint 不校验直接放行
        #    （只有 COOLAPK_ALLOW_ANONYMOUS=1 才会走到这条路径）
        scopes2 = []
        ep_open = _build_bearer_endpoint("", scopes2)
        ev = await _drive_asgi(ep_open, {**base_scope, "headers": [(b"authorization", b"Bearer anything")]})
        check("H5d 空token 免鉴权放行", len(scopes2) == 1, f"calls={len(scopes2)}")

    asyncio.run(run())


# ---- S1: scoring 质量分 / 作者分级 / 专长推断（v0.5.0） ----
def probe_s1():
    from coolapk_mcp import scoring as sc

    # 权重：点赞 + 评论×3 + 转发×2
    check("S1a post_score 权重", sc.post_score(6907, 11493, 59) == 41504,
          f"got {sc.post_score(6907, 11493, 59)}")
    check("S1b post_score 全零", sc.post_score(0, 0, 0) == 0)
    check("S1c discussion_ratio", sc.discussion_ratio(6907, 11493) == 1.66,
          f"got {sc.discussion_ratio(6907, 11493)}")
    check("S1d discussion_ratio 零赞不除零", sc.discussion_ratio(0, 5) == 0.0,
          f"got {sc.discussion_ratio(0, 5)}")

    # 作者分级：实测样本回放（莫离然然/d2u8/烟雨ovo/小号）
    check("S1e tier top（3万粉+10级）", sc.author_tier(11, 79166, 233) == "top",
          f"got {sc.author_tier(11, 79166, 233)}")
    check("S1f tier expert（1万粉+8级）", sc.author_tier(9, 13642, 497) == "expert",
          f"got {sc.author_tier(9, 13642, 497)}")
    check("S1g tier active（1000粉+5级）", sc.author_tier(5, 4750, 19) == "active",
          f"got {sc.author_tier(5, 4750, 19)}")
    check("S1h tier normal（粉丝不足）", sc.author_tier(5, 56, 4) == "normal",
          f"got {sc.author_tier(5, 56, 4)}")
    check("S1i 高赞低粉仍是 normal（帖子火≠作者权威）",
          sc.author_tier(5, 4750, 19) != "top")

    # 专长推断
    check("S1j expertise 命中解锁/刷机",
          sc.infer_expertise(
              "一个玩机佬，不定时更新小白教程，可远程刷机救砖等",
              ["小米QC解锁BL工具v2.2已更新 支持澎湃OS4一键解锁BL"],
          )[:2] == ["解锁/Root", "刷机/救砖"])
    check("S1k expertise 空输入返回空列表", sc.infer_expertise("", []) == [])
    check("S1l expertise 最多 3 项",
          len(sc.infer_expertise("解锁 root 刷机 救砖 模块 内核 美化 主题 省电 性能", [])) <= 3)

    # summarize_author / annotate_post
    prof = {"level": 11, "fans_num": 79166, "feed_num": 233, "bio": "玩机佬"}
    summ = sc.summarize_author(prof)
    check("S1m summarize_author 关键字段齐全",
          set(summ) >= {"author_tier", "author_tier_label", "is_expert",
                        "fans_per_feed", "expertise"},
          f"got {sorted(summ)}")
    check("S1n summarize_author fans_per_feed 计算",
          summ["fans_per_feed"] == 340, f"got {summ['fans_per_feed']}")
    check("S1o summarize_author feed_num=0 不除零（返回 0）",
          sc.summarize_author({"level": 3, "fans_num": 10, "feed_num": 0})["fans_per_feed"] == 0)

    item = {"like_num": 6912, "reply_num": 11493, "forward_num": 59}
    sc.annotate_post(item)
    # 6912 + 11493×3 + 59×2 = 41509
    check("S1p annotate_post 原地补分",
          item.get("post_score") == 41509 and item.get("discussion_ratio") == 1.66,
          f"got {item}")


# ---- S2: 作者白名单 / user_feeds 包装结构（v0.5.0） ----
def probe_s2():
    from coolapk_mcp.models import apply_tool_whitelist

    # author 白名单保留分级字段
    author_raw = {
        "uid": 10828415, "username": "莫离然然", "bio": "玩机佬", "level": 11,
        "fans_num": 79166, "follow_num": 5, "feed_num": 233, "city": "成都",
        "gender": "男", "verify_title": "", "reg_date": "2015-01-01",
        "author_tier": "top", "author_tier_label": "领域顶流", "is_expert": True,
        "fans_per_feed": 340, "expertise": ["刷机/救砖"],
        "cookie": "SECRET", "token": "SECRET2",
    }
    out = apply_tool_whitelist("author", author_raw)
    check("S2a author 保留 author_tier", out.get("author_tier") == "top", f"got {out}")
    check("S2b author 保留 is_expert/fans_per_feed",
          out.get("is_expert") is True and out.get("fans_per_feed") == 340)
    check("S2c author 保留 expertise", out.get("expertise") == ["刷机/救砖"])
    check("S2d author 剔除敏感字段",
          "cookie" not in out and "token" not in out, f"got {sorted(out)}")

    # author_digest 包装结构：元数据 + posts 走动态白名单
    digest_raw = {
        "uid": 10828415, "username": "莫离然然", "author": author_raw,
        "keyword": "解锁", "pages": 2, "fetched": 39, "matched": 6,
        "posts": [
            {"id": 69093934, "title": "解锁工具", "content": "正文",
             "like_num": 6912, "reply_num": 11493, "forward_num": 59,
             "post_score": 41530, "discussion_ratio": 1.66,
             "cookie": "SECRET", "userInfo": {"uid": 1}},
        ],
    }
    out2 = apply_tool_whitelist("author_digest", digest_raw)
    check("S2e author_digest 元数据保留",
          out2.get("fetched") == 39 and out2.get("matched") == 6, f"got {out2.get('fetched')}")
    check("S2f author_digest posts 保留 post_score",
          bool(out2.get("posts")) and out2["posts"][0].get("post_score") == 41530,
          f"got {out2.get('posts')}")
    check("S2g author_digest posts 剔除敏感字段",
          "cookie" not in out2["posts"][0] and "userInfo" not in out2["posts"][0])

    # user_feeds 新版包装结构（v0.5.0）与旧纯 list 兼容
    feeds_raw = {
        "uid": 10828415, "pages": 2, "fetched": 39, "matched": 29,
        "sort": "hot", "keyword": "解锁",
        "feeds": [
            {"id": 69093934, "title": "解锁工具", "like_num": 6912,
             "post_score": 41530, "discussion_ratio": 1.66, "cookie": "SECRET"},
            {},
        ],
    }
    out3 = apply_tool_whitelist("user_feeds", feeds_raw)
    check("S2h user_feeds 包装元数据保留",
          out3.get("pages") == 2 and out3.get("matched") == 29 and out3.get("sort") == "hot",
          f"got {out3.get('pages')}")
    check("S2i user_feeds 空条目被丢弃", len(out3.get("feeds") or []) == 1,
          f"got {len(out3.get('feeds') or [])}")
    check("S2j user_feeds 保留质量分",
          out3["feeds"][0].get("post_score") == 41530)
    legacy = apply_tool_whitelist("user_feeds", [{"id": 1, "like_num": 5, "cookie": "S"}])
    check("S2k user_feeds 旧纯 list 仍兼容",
          isinstance(legacy, list) and legacy and "cookie" not in legacy[0])




def probe_c3():
    """0.6.5 评论聚焦 / 链接提取 / 新字段白名单"""
    from coolapk_mcp.models import FeedDetail, ReplyModel, apply_tool_whitelist

    # 1) ReplyModel 链接提取
    r = ReplyModel.from_api({
        "id": 1, "message": "工具下载 https://pan.quark.cn/s/abc 提取码666，备用 https://github.com/x/y",
        "userInfo": {"uid": 5, "username": "u"},
    })
    check("C1 评论链接提取", r.links == ["https://pan.quark.cn/s/abc", "https://github.com/x/y"],
          f"got {r.links}")
    r2 = ReplyModel.from_api({"id": 2, "message": "普通评论没有链接", "userInfo": {}})
    check("C2 无链接评论 links 空", r2.links == [], f"got {r2.links}")
    r3 = ReplyModel.from_api({"id": 3, "message": "蓝奏云 wwl.lanzoui.com/ixyz 提取码x", "userInfo": {}})
    check("C3 裸域名链接识别", r3.links == ["wwl.lanzoui.com/ixyz"],
          f"got {r3.links}")

    # 2) FeedDetail 新字段 + 白名单
    d = FeedDetail.from_api(
        {"id": 9, "message": "正文", "userInfo": {"uid": 1}, "replynum": 100},
        [{"id": 1, "message": "楼主补充 https://pan.quark.cn/s/z", "isFeedAuthor": 1, "userInfo": {"uid": 1}}],
        fetch_source="api",
        comments_focus={"mode": "smart", "author_replies": 1, "hot_replies": 20, "total_shown": 21, "total_replynum": 100},
    )
    d.edit_history = [{"id": 1, "content": "v1", "dateline": 1}]
    d.sub_replies_expanded = 7
    out = d.model_dump(mode="json", exclude_defaults=True)
    wl = apply_tool_whitelist("feed_detail", out)
    check("C4 comments_focus 进白名单", wl.get("comments_focus", {}).get("mode") == "smart",
          f"got {wl.get('comments_focus')}")
    check("C5 edit_history 进白名单", len(wl.get("edit_history") or []) == 1,
          f"got {wl.get('edit_history')}")
    check("C6 sub_replies_expanded 进白名单", wl.get("sub_replies_expanded") == 7)
    check("C7 评论 links 进白名单",
          (wl.get("replies") or [{}])[0].get("links") == ["https://pan.quark.cn/s/z"],
          f"got {(wl.get('replies') or [{}])[0].get('links')}")

    # 3) ReplyModel 兼容：旧数据无 links 字段不炸
    r4 = ReplyModel(id=4, content="旧形态")
    check("C8 无 message 的旧构造不炸", r4.links == [])

    # 4) 环境变量 COOLAPK_MAX_REPLY_PAGES 边界
    import os
    os.environ.pop("COOLAPK_MAX_REPLY_PAGES", None)
    # get_all_replies 的钳位逻辑在方法内部，用签名验证（不发请求）
    import inspect
    from coolapk_mcp.client import CoolapkClient
    sig = inspect.signature(CoolapkClient.get_all_replies)
    check("C9 get_all_replies 保留 max_pages 参数", "max_pages" in sig.parameters)

    # 5) feed_detail 工具签名
    from coolapk_mcp import server as _srv
    for t in _srv.mcp._tool_manager._tools.values():
        if t.name == "coolapk_feed_detail":
            params = set(t.parameters.get("properties", {}).keys())
            need = {"comments_focus", "with_history", "expand_sub_replies"}
            check("C10 feed_detail 新参数已注册", need <= params, f"got {sorted(params)}")
            break




def probe_d1():
    """0.6.6 feed_detail shareUrl 输出"""
    from coolapk_mcp.models import FeedDetail, apply_tool_whitelist

    d = FeedDetail.from_api(
        {"id": 9, "message": "x", "userInfo": {"uid": 1},
         "shareUrl": "https://www.coolapk.com/feed/9?s=abc"},
        fetch_source="api",
    )
    check("D1 share_url 字段透传", d.share_url == "https://www.coolapk.com/feed/9?s=abc",
          f"got {d.share_url!r}")
    wl = apply_tool_whitelist("feed_detail", d.model_dump(mode="json", exclude_defaults=True))
    check("D2 share_url 进白名单",
          wl.get("share_url") == "https://www.coolapk.com/feed/9?s=abc",
          f"got {wl.get('share_url')!r}")
    d2 = FeedDetail.from_api({"id": 10, "message": "y", "userInfo": {}}, fetch_source="api")
    check("D3 无 shareUrl 回空串", d2.share_url == "")




def probe_d2():
    """0.6.7 download_image 缩略判定（离线纯函数）"""
    import os
    from coolapk_mcp.client import CoolapkClient

    big = "https://image.coolapk.com/feed/2026/1104/09/x@2351x3527.jpg"
    check("D4 大图缩略地址", CoolapkClient._thumb_fetch_url(big) == big + ".m.jpg")
    check("D5 小图同走缩略", CoolapkClient._thumb_fetch_url(
        "https://image.coolapk.com/feed/x@960x1280.jpg").endswith(".m.jpg"))
    check("D6 png 不走缩略", CoolapkClient._thumb_fetch_url(
        "https://image.coolapk.com/feed/x@2351x3527.png") is None)
    check("D7 无尺寸后缀不走缩略", CoolapkClient._thumb_fetch_url(
        "https://image.coolapk.com/feed/plain.jpg") is None)
    os.environ["COOLAPK_IMG_THUMBNAIL"] = "0"
    check("D8 开关关闭返回 None", CoolapkClient._thumb_fetch_url(big) is None)
    os.environ["COOLAPK_IMG_THUMBNAIL"] = "false"
    check("D9 false 同样关闭", CoolapkClient._thumb_fetch_url(big) is None)
    os.environ.pop("COOLAPK_IMG_THUMBNAIL", None)
    check("D10 默认开启", CoolapkClient._thumb_fetch_url(big) == big + ".m.jpg")


def probe_rh1():
    from coolapk_mcp.client import _extract_request_hash
    check("rh1 老格式 requestHash: 'x'", _extract_request_hash("var x = {requestHash: 'abc123'};") == "abc123")


def probe_rh2():
    from coolapk_mcp.client import _extract_request_hash
    html = "var authFlow = createAuthCaptchaFlow('/auth/login?type=mobile', 'ba13b6fcto4wie');"
    check("rh2 新格式 createAuthCaptchaFlow 第二参数", _extract_request_hash(html) == "ba13b6fcto4wie")


def probe_rh3():
    from coolapk_mcp.client import _extract_request_hash
    html = "function createAuthCaptchaFlow(url, requestHash) {"
    check("rh3 定义行不误命中/空串安全", _extract_request_hash(html) == "" and _extract_request_hash("") == "")


def probe_dg1():
    """D 组外链：签名复算 / 去重 / 逗号 URL / 空 base / ttl 夹取"""
    from coolapk_mcp import img_link as _il
    base = "https://mcp.example.com"
    raw = "https://image.coolapk.com/a.jpg?ids=1,2"
    payload = _il.build_image_links(raw + " " + raw, base + "/", "s3cret", 7, now=1000.0)
    link = payload["items"][0]["link"]
    params = httpx.URL(link).params
    empty = _il.build_image_links(raw, "  ", "s")
    check(
        "dg1 外链签名/去重/逗号 URL/空 base/ttl 夹取",
        payload["count"] == 1
        and payload["ttl_minutes"] == 7
        and link.startswith(base + "/img?")
        and params["u"] == raw
        and params["sig"] == _il.sign_image(raw, "s3cret", 1000 + 7 * 60)
        and empty["count"] == 0
        and "public_base" in empty["error"]
        and _il.clamp_ttl(float("inf")) == _il.DEFAULT_TTL_MINUTES
        and _il.clamp_ttl(True) == _il.DEFAULT_TTL_MINUTES,
        link,
    )


def probe_dg2():
    """D 组指标：调用数 / 重试 / 错误归类 / 延迟"""
    from coolapk_mcp import throttle as _throttle
    sent = []

    def handler(request):
        sent.append(str(request.url))
        if len(sent) == 1:
            return httpx.Response(200, json={"data": {"ok": 1}})
        return httpx.Response(500, json={"message": "boom"})

    orig_sleep = _throttle.backoff_sleep

    async def no_sleep(attempt, floor=0.0):
        return None

    _throttle.backoff_sleep = no_sleep
    client = make_client()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def run():
        try:
            await client.get("/v6/feed/detail?id=1")
            with contextlib.suppress(CoolapkError):
                await client.get("/v6/feed/detail?id=2")
            return client.metrics()
        finally:
            await client.close()

    try:
        metrics = asyncio.run(run())
    finally:
        _throttle.backoff_sleep = orig_sleep
    check(
        "dg2 指标记录调用/重试/错误分类/延迟",
        metrics["calls"] == 2
        and metrics["errors"].get("transient") == 1
        and metrics["retries"] >= 1
        and metrics["latency_ms"]["max"] >= metrics["latency_ms"]["avg"] > 0,
        json.dumps(metrics, ensure_ascii=False),
    )


def probe_dg3():
    """D 组日志：单行 JSON、非法级别回落、第三方级别压到 WARNING"""
    from coolapk_mcp import logging_utils as _log
    record = logging.LogRecord("probe", logging.ERROR, __file__, 1, "msg\u2028x", (), None)
    line = _log.JsonFormatter().format(record)
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    noisy = logging.getLogger("httpx")
    saved_noisy = noisy.level
    os.environ["COOLAPK_LOG_JSON"] = "1"
    os.environ["COOLAPK_LOG_LEVEL"] = "verbose"
    try:
        _log.setup_logging()
        fallback_ok = root.level == logging.INFO
        installed = any(getattr(h, "_coolapk_json", False) for h in root.handlers)
        noisy_ok = noisy.level == logging.WARNING
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        noisy.setLevel(saved_noisy)
        os.environ.pop("COOLAPK_LOG_JSON", None)
        os.environ.pop("COOLAPK_LOG_LEVEL", None)
    check(
        "dg3 JSON 单行 + 非法级别回落 INFO + 第三方压 WARNING",
        "\u2028" not in line and len(line.splitlines()) == 1 and fallback_ok and installed and noisy_ok,
        line[:120],
    )


def probe_dg4():
    """D 组 /status：配置损坏时降级而不是抛错"""
    class _Stub:
        def cache_stats(self):
            return {"entries": 0, "bytes": 0}

        def metrics(self):
            return {"requests": 0}

    descriptor = _srvmod.AppConfig.__dict__["load"]

    def boom(cls):
        raise OSError("config.json 损坏")

    _srvmod.AppConfig.load = classmethod(boom)
    try:
        payload = asyncio.run(_srvmod._status_payload(_Stub()))
    finally:
        _srvmod.AppConfig.load = descriptor
    check(
        "dg4 配置损坏仍返回 ok + login.mode=unknown",
        payload["status"] == "ok" and payload["login"]["mode"] == "unknown",
        json.dumps(payload, ensure_ascii=False)[:160],
    )

# ---- 用户可感知改进（第二批）：六项 ----

_UI_FEED = {
    "entityType": "feed",
    "message": "正文内容",
    "username": "作者",
    "dateline": 1700000000,
    "likenum": 3,
}


def _ui_feeds(count: int) -> list:
    return [dict(_UI_FEED, id=index + 1) for index in range(count)]


def _ui_use(fake):
    """把 server._get_client 换成返回 fake 的协程，返回原函数供恢复。"""
    original = _srvmod._get_client

    async def _fake():
        return fake

    _srvmod._get_client = _fake
    return original


class _UiHomeClient:
    def __init__(self, count: int) -> None:
        self.count = count
        self.calls = []

    async def get_main_feeds(self, page: int = 1):
        self.calls.append(("main", page))
        return _ui_feeds(self.count)

    async def get_home_feeds(self, page: int = 1):
        self.calls.append(("home", page))
        return _ui_feeds(self.count)

    async def get_hot_feeds(self, page: int = 1):
        self.calls.append(("hot", page))
        return _ui_feeds(self.count)

    async def get_latest_feeds(self, page: int = 1):
        self.calls.append(("latest", page))
        return _ui_feeds(self.count)


def probe_ui1():
    """首页分栏语义：recommend 走真推荐流、follow 独立、未知归属与回显 + 分页信号"""
    size = CoolapkClient.PAGE_SIZE
    expected = {
        "follow": ("home", "follow"),
        "关注": ("home", "follow"),
        "dynamic": ("home", "follow"),
        "hot": ("hot", "hot"),
        "latest": ("latest", "latest"),
    }
    bad = []
    for tab in ("recommend", "", "不存在的分栏", "follow", "关注", "dynamic", "hot", "latest"):
        source, shown = expected.get(tab, ("main", "recommend"))
        fake = _UiHomeClient(size)
        original = _ui_use(fake)
        try:
            payload = json.loads(asyncio.run(_srvmod.coolapk_home(tab, 2)))
        finally:
            _srvmod._get_client = original
        if not (
            fake.calls == [(source, 2)]
            and payload["tab"] == shown
            and payload["page"] == 2
            and payload["fetched"] == size
            and payload["has_more"] is True
            and payload["next_page"] == 3
        ):
            bad.append((tab, fake.calls, payload.get("tab"), payload.get("next_page")))
    check("ui1 首页分栏路由/回显/分页信号（8 组）", not bad, str(bad))

    fake = _UiHomeClient(size - 1)
    original = _ui_use(fake)
    try:
        short = json.loads(asyncio.run(_srvmod.coolapk_home("hot", 3)))
    finally:
        _srvmod._get_client = original
    check(
        "ui1b 未填满一页即末页（has_more=False / next_page=None）",
        short["has_more"] is False and short["next_page"] is None,
        json.dumps(short, ensure_ascii=False)[:140],
    )


class _UiBoardClient:
    def __init__(self, count: int) -> None:
        self.count = count
        self.calls = []

    async def get_board_feeds(self, board_id: str, page: int = 1):
        self.calls.append((board_id, page))
        return _ui_feeds(self.count)


class _UiNotifyClient:
    def __init__(self, count: int) -> None:
        self.count = count

    async def get_notifications(self, ntype: str, page: int = 1):
        return [{"id": index, "message": "回复", "dateline": 1700000000} for index in range(self.count)]


def probe_ui2():
    """列表类工具统一分页信号：board（含中文板块名）与 notify"""
    size = CoolapkClient.PAGE_SIZE
    board_id = next((bid for bid, (title, _u) in _srvmod.BOARD_URLS.items() if title == "数码"), None)
    fake = _UiBoardClient(size)
    original = _ui_use(fake)
    try:
        board = json.loads(asyncio.run(_srvmod.coolapk_board("数码", 1)))
    finally:
        _srvmod._get_client = original
    check(
        "ui2 板块工具分页信号 + 中文板块名",
        board_id is not None
        and board["board"] == board_id
        and board["has_more"] is True
        and board["next_page"] == 2
        and fake.calls == [(board_id, 1)],
        json.dumps(board, ensure_ascii=False)[:140],
    )

    fake2 = _UiNotifyClient(size)
    original2 = _ui_use(fake2)
    try:
        notify = json.loads(asyncio.run(_srvmod.coolapk_notify("message", 1)))
    finally:
        _srvmod._get_client = original2
    check(
        "ui2b 通知工具分页信号",
        notify.get("has_more") is True and notify.get("next_page") == 2,
        json.dumps(notify, ensure_ascii=False)[:140],
    )


class _UiSearchClient:
    def __init__(self, items: list) -> None:
        self.items = items

    async def search(self, keyword, search_type="feed", page=1, sort="hot"):
        return self.items


def _ui_search_items(matched: int, unmatched: int) -> list:
    items = [dict(_UI_FEED, id=index + 1, message="Magisk 刷机教程") for index in range(matched)]
    items += [dict(_UI_FEED, id=100 + index, message="无关内容") for index in range(unmatched)]
    return items


def probe_ui3():
    """搜索空结果必须解释原因：被过滤 / 只剩卡片 / 真没有；有结果时不得带 hint"""
    fake = _UiSearchClient(_ui_search_items(2, 0))
    original = _ui_use(fake)
    try:
        payload = json.loads(asyncio.run(_srvmod.coolapk_search("Magisk", "feed", 1)))
    finally:
        _srvmod._get_client = original
    no_hint = "hint" not in payload

    fake2 = _UiSearchClient(_ui_search_items(0, 30))
    original2 = _ui_use(fake2)
    try:
        filtered = json.loads(asyncio.run(_srvmod.coolapk_search("Magisk", "feed", 1)))
    finally:
        _srvmod._get_client = original2

    fake3 = _UiSearchClient([])
    original3 = _ui_use(fake3)
    try:
        empty = json.loads(asyncio.run(_srvmod.coolapk_search("冷门词", "feed", 1)))
    finally:
        _srvmod._get_client = original3

    check(
        "ui3 搜索空结果三分支解释 + 有结果不带 hint",
        no_hint
        and filtered["results"] == []
        and filtered["counts"]["keyword_filtered"] == 30
        and "strict=false" in filtered["hint"]
        and "没有搜到" in empty["hint"],
        json.dumps(filtered, ensure_ascii=False)[:160],
    )


class _UiDetailClient:
    def __init__(self) -> None:
        self.seen = []

    async def get_feed_detail_smart(self, feed_id, **kwargs):
        self.seen.append(feed_id)
        return {"data": {"id": feed_id, "message": "正文", "replynum": 0}, "replies": None, "source": "api"}


def probe_ui4():
    """分享链接解析：数字/链接/?id=/feedId=/整段文案，多 ID 与无 ID 必须拒绝"""
    from coolapk_mcp.linkparse import parse_feed_id

    cases = [
        (123, 123),
        ("123456", 123456),
        ("https://www.coolapk.com/feed/1234567", 1234567),
        ("看看这个 https://www.coolapk.com/feed/7654321 挺好", 7654321),
        ("https://www.coolapk.com/m/feed/detail?id=8899", 8899),
        ("https://www.coolapk.com/feed/detail?feedId=4242", 4242),
        ("#在酷安# 分享自酷安 https://www.coolapk.com/feed/31337?shareFrom=1", 31337),
        (None, None),
        (True, None),
        ("没有数字", None),
        ("看这两条 1234567 和 7654321", None),
    ]
    bad = [(value, parse_feed_id(value), want) for value, want in cases if parse_feed_id(value) != want]

    fake = _UiDetailClient()
    original = _ui_use(fake)
    try:
        detail = json.loads(asyncio.run(_srvmod.coolapk_feed_detail("https://www.coolapk.com/feed/1234567890")))
        rejected = json.loads(asyncio.run(_srvmod.coolapk_feed_detail("看这两条 1234567 和 7654321")))
    finally:
        _srvmod._get_client = original
    check(
        "ui4 分享链接解析（11 组）+ 工具接受链接、拒绝歧义文本",
        not bad
        and fake.seen == [1234567890]
        and isinstance(detail, dict)
        and "error" not in detail
        and "无法从 feed_id 解析" in str(rejected.get("error", "")),
        f"bad={bad} seen={fake.seen} detail={str(detail)[:80]} rejected={rejected}",
    )


class _UiImageClient:
    def __init__(self) -> None:
        self.seen = []

    async def download_image(self, url: str):
        self.seen.append(url)
        return b"\x89PNG-fake", "png"


def probe_ui5():
    """多图读取：一次多张、非法域名先拦不下后续、超 9 张与空输入报错、非结构化返回"""
    fake = _UiImageClient()
    original = _ui_use(fake)
    try:
        batch = asyncio.run(
            _srvmod.coolapk_read_image("https://image.coolapk.com/a.png, https://image.coolapk.com/b.jpg")
        )
        foreign = json.loads(
            asyncio.run(
                _srvmod.coolapk_read_image("https://evil.example.com/a.png https://image.coolapk.com/b.png")
            )
        )
        too_many = json.loads(
            asyncio.run(
                _srvmod.coolapk_read_image(" ".join(f"https://image.coolapk.com/{i}.png" for i in range(10)))
            )
        )
        blank = json.loads(asyncio.run(_srvmod.coolapk_read_image("   ")))
    finally:
        _srvmod._get_client = original
    check(
        "ui5 多图读取/域名先校验/上限/空输入",
        len(batch) == 2
        and fake.seen == ["https://image.coolapk.com/a.png", "https://image.coolapk.com/b.jpg"]
        and foreign["code"] == 403
        and "最多 9 张" in too_many["error"]
        and "不能为空" in blank["error"],
        f"batch={len(batch)} seen={fake.seen} foreign={foreign}",
    )

    tool = _mcp_inst._tool_manager.get_tool("coolapk_read_image")
    fake2 = _UiImageClient()
    original2 = _ui_use(fake2)
    try:
        result = asyncio.run(
            _mcp_inst.call_tool(
                "coolapk_read_image",
                {"url": "https://image.coolapk.com/a.png, https://image.coolapk.com/b.jpg"},
            )
        )
    finally:
        _srvmod._get_client = original2
    content = result[0] if isinstance(result, tuple) else result
    check(
        "ui5b 工具 schema 非结构化（output_schema=None）且返回两块 ImageContent",
        getattr(tool, "output_schema", "missing") is None
        and [type(item).__name__ for item in content] == ["ImageContent", "ImageContent"],
        f"schema={getattr(tool, 'output_schema', 'missing')} content={[type(i).__name__ for i in content]}",
    )


def probe_ui6():
    """长文本上限：正文/评论截断标 content_truncated，回复条数上限标 replies_omitted，可关闭"""
    from coolapk_mcp import models as _models
    from coolapk_mcp.models import FeedDetail, FeedModel, ReplyModel, cap_replies

    long_feed = FeedModel.from_api(dict(_UI_FEED, message="长" * 5000))
    short_feed = FeedModel.from_api(dict(_UI_FEED, message="短正文"))
    long_reply = ReplyModel.from_api({"id": 9, "message": "评" * 3000, "dateline": 1700000000})
    detail = FeedDetail.from_api(
        {"id": 1, "message": "正文"},
        [{"id": index + 1, "message": f"评论{index}"} for index in range(100)],
    )
    capped = cap_replies(detail)
    check(
        "ui6 正文/评论截断标 content_truncated + 回复条数标 replies_omitted",
        long_feed.content_truncated is True
        and len(long_feed.content) <= _models.FEED_CONTENT_MAX_CHARS + 1
        and long_feed.content.endswith("…")
        and short_feed.content_truncated is False
        and short_feed.content == "短正文"
        and long_reply.content_truncated is True
        and len(capped.replies) == _models.REPLY_MAX_ITEMS
        and capped.replies_omitted == 100 - _models.REPLY_MAX_ITEMS
        and capped.replies_truncated is True,
        f"feed={len(long_feed.content)} reply={len(long_reply.content)} omitted={capped.replies_omitted}",
    )

    before = (_models.FEED_CONTENT_MAX_CHARS, _models.REPLY_CONTENT_MAX_CHARS, _models.REPLY_MAX_ITEMS)
    os.environ["COOLAPK_FEED_CONTENT_MAX"] = "7"
    os.environ["COOLAPK_REPLY_CONTENT_MAX"] = "bad"
    os.environ["COOLAPK_REPLY_MAX_ITEMS"] = "0"
    try:
        importlib.reload(_models)
        reloaded = (_models.FEED_CONTENT_MAX_CHARS, _models.REPLY_CONTENT_MAX_CHARS, _models.REPLY_MAX_ITEMS)
    finally:
        for key in ("COOLAPK_FEED_CONTENT_MAX", "COOLAPK_REPLY_CONTENT_MAX", "COOLAPK_REPLY_MAX_ITEMS"):
            os.environ.pop(key, None)
        importlib.reload(_models)
    check(
        "ui6b 三项上限可由环境变量配置（非法值回落默认、0 表示不限制）",
        reloaded == (7, 800, 0) and _models._truncate("abc", 0) == ("abc", False)
        and _models._truncate("abcdef", 3) == ("abc…", True)
        and (_models.FEED_CONTENT_MAX_CHARS, _models.REPLY_CONTENT_MAX_CHARS, _models.REPLY_MAX_ITEMS) == before,
        f"reloaded={reloaded} restored={before}",
    )


def main():
    probe_b1()
    probe_b2()
    probe_b2_ok()
    probe_a1()
    probe_get_url()
    probe_c2()
    probe_c1()
    probe_d3()
    probe_e2()
    probe_strip()
    # v0.3.0 HTTP 传输层用例
    probe_h1()
    probe_h2()
    probe_h3()
    probe_h4()
    probe_h5()
    probe_h6()
    # v0.5.0 作者质量锚定用例
    probe_s1()
    probe_s2()
    probe_c3()
    probe_d1()
    probe_d2()
    probe_rh1()
    probe_rh2()
    probe_rh3()
    # D 组（工程化收尾）能力用例
    probe_dg1()
    probe_dg2()
    probe_dg3()
    probe_dg4()
    # 用户可感知改进（第二批）能力用例
    probe_ui1()
    probe_ui2()
    probe_ui3()
    probe_ui4()
    probe_ui5()
    probe_ui6()
    print(f"\n==== 回归结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
    if FAIL:
        print("失败项:")
        for f in FAIL:
            print(f"  - {f}")
        sys.exit(1)


if __name__ == "__main__":
    main()
