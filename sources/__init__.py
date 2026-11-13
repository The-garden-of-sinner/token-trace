"""多平台数据源。

每个模块负责「一个客户端的数据 → token-trace 的 calls / ctx 契约」：
    zcode.py   ZCode（Z.ai / GLM）：SQLite 主源 + model-io 补充源
后续 workbuddy.py / codex.py 按同一形状加入，gen_trace 只留纯函数与聚合。
"""

__all__ = ["zcode"]
