"""极简 HTTP 工具：GET 带重试，POST JSON / 表单。只用标准库。

安全与效率约定：
- 4xx（除 429）不重试，立刻失败；429/5xx/网络错误退避重试。
- 报错里的 URL 自动脱敏（api_key/token 等参数打码），避免密钥进日志。
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (compatible; lit-zaobao/0.4; +personal literature digest bot)"

_SECRET = re.compile(r"((?:api_?)?key|token|sendkey|secretkey|secret)=([^&\s]+)", re.IGNORECASE)


def redact_url(url: str) -> str:
    return _SECRET.sub(r"\1=***", url)


def _backoff(attempt: int, retry_after: str | None = None) -> float:
    """第 attempt 次失败后的等待秒数；429 优先听服务器的 Retry-After。"""
    if retry_after:
        try:
            return max(float(retry_after), 2.0)
        except ValueError:
            pass
    return min(30.0, 4.0 * (attempt + 1))  # 4s, 8s, 12s...


def http_get(url: str, params: dict | None = None, timeout: int = 25,
             retries: int = 2, headers: dict | None = None) -> str:
    if params:
        sep = "&" if "?" in url else "?"
        url = url + sep + urllib.parse.urlencode(params)
    safe_url = redact_url(url)
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")[:80].strip()
            except Exception:  # noqa: BLE001 - 读错误体失败不影响重试
                pass
            detail = str(e.reason or "").strip()
            if body and (not detail or detail.lower().startswith("unknown")):
                detail = f"{detail}: {body}" if detail else body
            last_err = RuntimeError(f"HTTP {e.code}: {detail}")
            if e.code != 429 and e.code < 500:
                break  # 客户端错误重试无意义
            if e.code == 429 and attempt < retries:
                time.sleep(_backoff(attempt, e.headers.get("Retry-After")))
                continue
        except Exception as e:  # noqa: BLE001 - 超时/断连统一退避重试
            last_err = e
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"GET 失败: {safe_url[:120]} :: {last_err}")


def http_post(url: str, payload: dict, *, form: bool = False,
              timeout: int = 30, headers: dict | None = None) -> str:
    if form:
        data = urllib.parse.urlencode(payload).encode()
        ctype = "application/x-www-form-urlencoded"
    else:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        ctype = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data,
                                 headers={"User-Agent": UA, "Content-Type": ctype, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")
