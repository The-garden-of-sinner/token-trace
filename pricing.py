#!/usr/bin/env python3
"""按公开刊例价估算一次调用花了多少钱（人民币元）。

用途：跨平台排序与横向对比。**这是估算，不是账单**——订阅套餐、限时折扣、
长上下文加价都不会体现在这里。WorkBuddy 的真实积分仍由 credit 字段表示，
本模块只回答「同样的 token 按 API 价该收多少」。

计费三档与 token-trace 契约一致：fresh 输入 = pin - hit，缓存命中 = hit，
输出 = pout（reasoning 已含在输出里，不重复计）。

价格为「元 / 百万 token」：(输入, 缓存命中, 输出)。美元价按 FX_USD_CNY 折算。
未收录的模型走 FALLBACK，并在 price_src 里明确标记 "fallback"，不冒充官方价。
"""

FX_USD_CNY = 7.1

RATES = {
    # 腾讯混元（腾讯云 TokenHub 刊例价，2026-11）
    "hy4": (6.0, 0.3, 18.0, "腾讯云 TokenHub"),
    "hy4-preview": (6.0, 0.3, 18.0, "腾讯云 TokenHub"),
    # 智谱 GLM（开放平台刊例价，2026-11）
    "glm-5.3": (8.0, 2.0, 28.0, "智谱开放平台"),
    "glm-5.3-flash": (0.8, 0.23, 2.8, "智谱开放平台"),
    "glm-5.2": (8.0, 2.0, 28.0, "智谱开放平台"),
    "glm-5.1": (6.0, 1.3, 24.0, "智谱开放平台（32K 内）"),
    # DeepSeek（官方 off-peak：$0.15 / $0.003 / $0.60；峰值时段翻倍）
    "deepseek-v4.1-flash": (0.15 * FX_USD_CNY, 0.003 * FX_USD_CNY, 0.60 * FX_USD_CNY,
                            "DeepSeek 官方（off-peak）"),
    "deepseek-v4-flash": (0.15 * FX_USD_CNY, 0.003 * FX_USD_CNY, 0.60 * FX_USD_CNY,
                          "DeepSeek 官方（off-peak）"),
    "deepseek-flash": (0.15 * FX_USD_CNY, 0.003 * FX_USD_CNY, 0.60 * FX_USD_CNY,
                       "DeepSeek 官方（off-peak）"),
    # 蚂蚁百灵（$0.30 / $0.90；官方未公布缓存价，按输入价计）
    "ling-3.2-flash": (0.30 * FX_USD_CNY, 0.30 * FX_USD_CNY, 0.90 * FX_USD_CNY,
                       "蚂蚁百灵（无缓存折扣）"),
    # OpenAI（$ / 1M × FX）
    "gpt-6-luna": (0.10 * FX_USD_CNY, 0.01 * FX_USD_CNY, 0.50 * FX_USD_CNY, "OpenAI 价目"),
    "gpt-6.1-sol": (2.0 * FX_USD_CNY, 0.10 * FX_USD_CNY, 10.0 * FX_USD_CNY, "OpenAI 价目"),
    "gpt-6.1-terra": (1.0 * FX_USD_CNY, 0.05 * FX_USD_CNY, 5.0 * FX_USD_CNY, "OpenAI 价目"),
    "gpt-6-astra": (10.0 * FX_USD_CNY, 1.0 * FX_USD_CNY, 50.0 * FX_USD_CNY, "OpenAI 价目"),
    "gpt-5.6-sol": (4.0 * FX_USD_CNY, 0.40 * FX_USD_CNY, 20.0 * FX_USD_CNY, "OpenAI 价目"),
    # Anthropic（WorkBuddy 可能出现的模型，按公开价近似）
    "claude-sonnet-5": (2.0 * FX_USD_CNY, 0.20 * FX_USD_CNY, 10.0 * FX_USD_CNY, "Anthropic 价目"),
    "claude-opus-4.8": (5.0 * FX_USD_CNY, 0.50 * FX_USD_CNY, 25.0 * FX_USD_CNY, "Anthropic 价目"),
}
# 未收录模型的中端保守档：只用于排序，不代表任何厂商报价。
FALLBACK = (4.0, 1.0, 12.0, "未收录，按中端档估算")


def canon(name):
    """模型名归一：大小写、空格、下划线不影响匹配；点号保留（gpt-6.1-sol）。"""
    s = (name or "").strip().lower()
    for ch in (" ", "_", "/", ":"):
        s = s.replace(ch, "-")
    while "--" in s:
        s = s.replace("--", "-")
    return s.strip("-")


def rates_for(model):
    """返回 (输入, 缓存命中, 输出, 来源)；未收录返回 FALLBACK。"""
    k = canon(model)
    if not k:
        return None
    hit = RATES.get(k)
    if hit:
        return hit
    for key in sorted(RATES, key=len, reverse=True):  # 长 key 优先，避免 glm-5.3 抢 glm-5.3-flash
        if k.startswith(key):
            return RATES[key]
    return FALLBACK


def cost_cny(model, pin, hit, pout):
    """一次调用的估算成本（元）。model 为空返回 None，其余一律给数（含 fallback）。"""
    r = rates_for(model)
    if not r:
        return None
    pin, hit, pout = int(pin or 0), int(hit or 0), int(pout or 0)
    hit = max(0, min(hit, pin))
    fresh = pin - hit
    return (fresh * r[0] + hit * r[1] + pout * r[2]) / 1_000_000.0


def price_src(model):
    """价格来源标签：list 表示命中刊例价，fallback 表示估算档。"""
    r = rates_for(model)
    if not r:
        return "none"
    return "fallback" if r is FALLBACK else "list"


if __name__ == "__main__":
    cases = [
        ("Hy4", 1_000_000, 0, 0, 6.0),
        ("Hy4", 1_000_000, 1_000_000, 0, 0.3),
        ("Hy4 preview", 0, 0, 1_000_000, 18.0),
        ("GLM-5.3", 1_000_000, 0, 0, 8.0),
        ("GLM-5.3-Flash", 1_000_000, 500_000, 1_000_000, 0.8 * 0.5 + 0.23 * 0.5 + 2.8),
        ("gpt-6-luna", 1_000_000, 0, 0, 0.71),
        ("gpt-6.1-sol", 1_000_000, 1_000_000, 0, 0.71),
        ("DeepSeek-V4.1-Flash", 1_000_000, 0, 0, 1.065),
        ("Ling-3.2-flash", 0, 0, 1_000_000, 6.39),
        ("Step-5-Preview", 1_000_000, 0, 0, 4.0),
    ]
    for model, pin, hit, pout, want in cases:
        got = cost_cny(model, pin, hit, pout)
        ok = abs(got - want) < 1e-6
        print(f"{'PASS' if ok else 'FAIL'}  {model:20s} ¥{got:.4f} 期望 ¥{want:.4f} "
              f"src={price_src(model)}")
        assert ok, model
    print("pricing 自测全过")
