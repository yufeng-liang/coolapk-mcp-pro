# 手动探针（不进 CI）

本目录存放需要人工触发的一次性回归探针，**不被 pytest 收集**（文件名不以 `test_` 开头）。

- `regression_probe.py`：离线 mock 探针，覆盖历史审查修复项（顶层 list 直通、
  风控分支、tag 编码、get_url 双重编码绕过、黑名单不误伤 replyList、桌面登录
  解析容错、质量分与作者分级、白名单包装结构等）。默认零真实网络请求。

运行：

```bash
python tests/manual/regression_probe.py
```

日常回归请用 `pytest`；探针只用于排查"某次修复是否真的还在"这类跨版本问题。
