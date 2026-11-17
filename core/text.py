# -*- coding: utf-8 -*-
"""文本处理与时间戳归一化公共工具函数。

各数据源与主服务共享同一套口径：
- 时间戳归一化到毫秒数值，秒级自动转换，越界与脏值安全降级；
- Token 估算：CJK 汉字与 Unicode 增补平面（Emoji / 罕用字）1 字 ≈ 1 token，
  ASCII 与西文字符 4 字符 ≈ 1 token；
- 本地日期字符串转换：闭区间校验防 Windows CRT 越界抛错。
"""

import datetime
import re
import time

_CJK_RE = re.compile(
    r"[\u2e80-\u9fff\uac00-\ud7af\uf900-\ufaff\U00010000-\U0010ffff]")


def _ms(v):
    """timestamp 归一成毫秒数值：int / float / 字符串 / None 都接受，非正值记 0。

    外部日志与数据库中时间戳可能为秒（如 17...）或毫秒（17...000），字符串或浮点数。
    Codex rollout 等来源直接用 ISO8601 字符串（如 2026-10-25T13:03:11.364Z），
    这里一并解析，避免各源各写一份局部 _ms 造成口径漂移。
    下游做相减、聚合与最大值计算时需保证类型一致。
    """
    try:
        x = float(v)
    except (TypeError, ValueError):
        # 非纯数字字符串：按 ISO8601 解析（Codex rollout 用 2026-10-25T13:03:11.364Z）。
        # 必须放在 float 失败之后——ISO 字符串也以数字开头，不能靠前缀判断。
        try:
            return int(datetime.datetime.fromisoformat(
                str(v).strip().replace("Z", "+00:00")).timestamp() * 1000)
        except Exception:
            return 0
    if not (x > 0):
        return 0
    if 0 < x < 1e11:     # 秒级时间戳转为毫秒
        x *= 1000.0
    return int(x) if x == int(x) else x


def est_tokens(s):
    """粗略估算原文 token 数：CJK 按 1 字 ≈ 1 token，其余按 4 字符 ≈ 1 token。

    只用于展示与排序，页面统一带 ``≈`` 标注，不冒充官方 tokenizer。
    """
    if not s:
        return 0
    cjk = sum(1 for _ in _CJK_RE.finditer(s))
    other = len(s) - cjk
    return cjk + (other + 3) // 4


def day_key(v):
    """把时间戳换成本地日期 YYYY-MM-DD，无效值返回空串。

    毫秒与秒均可接受；范围校验挡住 Windows CRT 对越界 localtime 抛 OSError(Errno 22)。
    """
    try:
        n = float(v)
        if not (0 < n < 1e14):
            return ""
        sec = n / 1000.0 if n > 1e11 else n
        if not (0 < sec < 32503680000):
            return ""
        return time.strftime("%Y-%m-%d", time.localtime(sec))
    except (OSError, ValueError, OverflowError, TypeError):
        return ""
