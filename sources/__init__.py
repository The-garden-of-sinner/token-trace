"""多平台数据源。

每个模块负责「一个客户端的数据 → token-trace 的 calls / ctx 契约」：
    zcode.py   ZCode（Z.ai / GLM）：SQLite 主源 + model-io 补充源
    codex.py   Codex CLI：rollout JSONL / .zst
    dsh.py     DeepSeek Harness：session.v4.jsonl.zstd 事件流
    _zstd.py   跨平台 zstd 解压（支持追加写的多帧文件），codex / dsh 共用
gen_trace 只留纯函数与聚合。
"""

__all__ = ["zcode", "codex", "dsh"]
