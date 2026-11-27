#!/usr/bin/env python3
"""按公开刊例价估算一次调用花了多少钱（人民币元）。

用途：跨平台排序与横向对比。**这是估算，不是账单**——订阅套餐、限时折扣、
峰谷分时、长上下文加价都不会体现在这里。WorkBuddy 的真实积分仍由 credit
字段表示，本模块只回答「同样的 token 按 API 价该收多少」。

计费三档与 token-trace 契约一致：fresh 输入 = pin - hit，缓存命中 = hit，
输出 = pout（reasoning 已含在输出里，不重复计）。

价格为「元 / 百万 token」：(输入, 缓存命中, 输出)。美元价按 FX_USD_CNY 折算。
未收录的模型走 FALLBACK，并在 price_src 里明确标记 "fallback"，不冒充官方价。

价目表核对日期 **2026-11-14**（Gemini / Sonnet 5.5 / GPT-6.2 / hy4-flash / Hy3 增补 2026-11-27），逐条来源见 RATES 的第四列：
  - 腾讯混元 hy4 ............ tencent.com 发布稿 / 腾讯云 TokenHub（6 / 0.3 / 18）
  - 智谱 GLM-5.3 / Flash ... bigmodel.cn/pricing、docs.bigmodel.cn 定价页
  - DeepSeek V4.1 Flash .... api-docs.deepseek.com 中文定价页（人民币价）
  - 蚂蚁百灵 Ling-3.2 ...... 官方发布 / IT之家（$0.30 / $0.90，缓存价未公布）
  - OpenAI GPT-6 系列 ...... platform.openai.com/docs/pricing、openai.com 商务定价页
  - Anthropic Claude ....... Anthropic 价目页（第三方价目表交叉核对）
  - Gemini 3.8 Flash ....... DeepMind 发布稿 / Google Cloud 价目页（$0.75 / $0.075 / $3.75，引导价至 2026-12-31）
  - Gemini 4 Neon .......... Google Cloud 模型页 / 发布稿（$3.00 / $0.15 / $15.00，2026-11-18 发布）
  - Claude Sonnet 5.5 ...... anthropic.com 发布稿（$2 / $0.10 / $10，缓存读 10-07 起减半）
  - OpenAI GPT-6.2 系列 .... platform.openai.com/docs/pricing（Astra $8 / $0.40 / $40，Sol $2 / $0.10 / $10）
  - 混元 hy4-flash / Hy3 ... 腾讯云 TokenHub 刊例价（0.5 / 0.02 / 2；1 / 0.25 / 4，人民币）
  - 汇率 .................. 2026-11-14 美元中间价约 6.6726（上海银行 6.6732 /
                              浦发 6.6718 / 国际即时 6.6726），取 6.67

已知时点差异（不进计算，仅提示读表的人）：
  - Hy4 在腾讯云 TokenHub 至 2026-12-31 按刊例价五折；这里按全价
  - hy4-flash 另有一条五折至 2026-11-30 12:00；这里同样按全价
  - GPT-5.6 Sol 促销价至 2026-11-30；到期后可能上调
  - Ling-3.2-flash 上线前两周免费（约至 2026-11-19）
  - DeepSeek 分峰谷：北京时间工作日 9:00-12:00、14:00-18:00 为高峰，
    三档价格全部翻倍。本表固定取**空闲时段**价，因此工作日下午的调用会低估
"""

FX_USD_CNY = 6.67     # 2026-11-14 美元兑人民币中间价 6.6726，见模块 docstring

RATES = {
    # 腾讯混元（腾讯云 TokenHub 刊例价，2026-11）
    "hy4": (6.0, 0.3, 18.0, "腾讯云 TokenHub"),
    "hy4-preview": (6.0, 0.3, 18.0, "腾讯云 TokenHub"),
    # hy4-flash 与 hy4 同族但价差 12 倍：不单列会被前缀匹配成 hy4 档。
    "hy4-flash": (0.5, 0.02, 2.0, "腾讯云 TokenHub 刊例价（hy4-flash）"),
    "hy3": (1.0, 0.25, 4.0, "腾讯云 TokenHub 刊例价（Hy3）"),
    # 智谱 GLM（bigmodel.cn/pricing 与官方定价文档，2026-11-14 核对）
    "glm-5.3": (8.0, 2.0, 28.0, "智谱开放平台"),
    "glm-5.3-flash": (0.8, 0.23, 2.8, "智谱开放平台"),
    # 这两条是沿用上一代档位的近似值，没有单独的官方价可核，标记为 approx
    "glm-5.2": (8.0, 2.0, 28.0, "沿用 GLM-5.3 档（近似）"),
    "glm-5.1": (6.0, 1.3, 24.0, "沿用上一代 32K 档（近似）"),
    # DeepSeek 官方中文定价页直接给人民币价，别用美元折算再乘汇率（会高估 6%）；
    # 这里取空闲时段：命中 0.02 / 未命中 1 / 输出 4 元每百万；高峰时段全部 ×2。
    "deepseek-v4.1-flash": (1.0, 0.02, 4.0, "DeepSeek 官方中文页（空闲时段）"),
    "deepseek-v4-flash": (1.0, 0.02, 4.0, "DeepSeek 官方中文页（空闲时段）"),
    "deepseek-flash": (1.0, 0.02, 4.0, "DeepSeek 官方中文页（空闲时段）"),
    # 蚂蚁百灵（$0.30 / $0.90，官方未公布缓存价，保守按输入价计）
    "ling-3.2-flash": (0.30 * FX_USD_CNY, 0.30 * FX_USD_CNY, 0.90 * FX_USD_CNY,
                       "蚂蚁百灵（缓存价未公布，按输入价计）"),
    # OpenAI（$ / 1M × FX）
    "gpt-6-luna": (0.10 * FX_USD_CNY, 0.01 * FX_USD_CNY, 0.50 * FX_USD_CNY, "OpenAI 价目"),
    "gpt-6.1-sol": (2.0 * FX_USD_CNY, 0.10 * FX_USD_CNY, 10.0 * FX_USD_CNY, "OpenAI 价目"),
    # GPT-6.2（2026-11-17 发布）：Astra 比 6 Astra 便宜两成，Sol 与 6.1 Sol 同价。
    "gpt-6.2-astra": (8.0 * FX_USD_CNY, 0.40 * FX_USD_CNY, 40.0 * FX_USD_CNY, "OpenAI 价目（6.2 Astra 标准层）"),
    "gpt-6.2-sol": (2.0 * FX_USD_CNY, 0.10 * FX_USD_CNY, 10.0 * FX_USD_CNY, "OpenAI 价目（6.2 Sol 标准层）"),
    "gpt-6.1-terra": (1.0 * FX_USD_CNY, 0.05 * FX_USD_CNY, 5.0 * FX_USD_CNY, "OpenAI 价目"),
    "gpt-6-astra": (10.0 * FX_USD_CNY, 1.0 * FX_USD_CNY, 50.0 * FX_USD_CNY, "OpenAI 价目"),
    "gpt-5.6-sol": (4.0 * FX_USD_CNY, 0.40 * FX_USD_CNY, 20.0 * FX_USD_CNY, "OpenAI 价目"),
    # Anthropic（WorkBuddy 可能出现的模型，按公开价近似）
    "claude-sonnet-5": (2.0 * FX_USD_CNY, 0.20 * FX_USD_CNY, 10.0 * FX_USD_CNY, "Anthropic 价目"),
    "claude-opus-4.8": (5.0 * FX_USD_CNY, 0.50 * FX_USD_CNY, 25.0 * FX_USD_CNY, "Anthropic 价目"),
    "claude-sonnet-5-5": (2.0 * FX_USD_CNY, 0.10 * FX_USD_CNY, 10.0 * FX_USD_CNY,
                          "Anthropic 价目（Sonnet 5.5，缓存读 10-07 起减半）"),
    "claude-sonnet-5-5-medium": (2.0 * FX_USD_CNY, 0.10 * FX_USD_CNY, 10.0 * FX_USD_CNY,
                                 "Anthropic 价目（Sonnet 5.5 medium 推理档，同标准价）"),
    # 官方 API ID 用连字符；保留点号别名，别处写成 sonnet-5.5 时不漏匹配。
    "claude-sonnet-5.5": (2.0 * FX_USD_CNY, 0.10 * FX_USD_CNY, 10.0 * FX_USD_CNY,
                          "Anthropic 价目（Sonnet 5.5，点号别名）"),
    "claude-sonnet-5.5-medium": (2.0 * FX_USD_CNY, 0.10 * FX_USD_CNY, 10.0 * FX_USD_CNY,
                                 "Anthropic 价目（Sonnet 5.5 medium，点号别名）"),
    # Google Gemini（Antigravity 实际使用的模型，2026-11-27 核对）
    "gemini-3.8-flash": (0.75 * FX_USD_CNY, 0.075 * FX_USD_CNY, 3.75 * FX_USD_CNY,
                         "Google 刊例价（3.8 Flash 引导价至 2026-12-31）"),
    "gemini-4-neon": (3.00 * FX_USD_CNY, 0.15 * FX_USD_CNY, 15.0 * FX_USD_CNY,
                      "Google 刊例价（4 Neon，2026-11-18 发布）"),
    # 这两个后缀是 Antigravity 内部的路由 / 计费变体，没有单独公开价；
    # 按同族标准档近似，price_src 会标 approx，不冒充官方价。
    "gemini-3.8-flash-n": (0.75 * FX_USD_CNY, 0.075 * FX_USD_CNY, 3.75 * FX_USD_CNY,
                           "Antigravity 路由后缀，按 3.8 Flash 标准档近似"),
    "gemini-3.8-flash-tiered": (0.75 * FX_USD_CNY, 0.075 * FX_USD_CNY, 3.75 * FX_USD_CNY,
                                "Antigravity 分级计费后缀，按 3.8 Flash 标准档近似"),
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


# 长 key 优先，避免 glm-5.3 抢 glm-5.3-flash。原先每次调用 rates_for 都要
# 现排一次 17 个 key——/stats 一次要算 1.7 万次调用，光排序就占几十毫秒。
_KEYS_BY_LEN = sorted(RATES, key=len, reverse=True)
_RATE_CACHE = {}       # 归一后的模型名 -> 档位元组，条目数等于模型种类数
_CANON_CACHE = {}


def _canon_cached(name):
    k = _CANON_CACHE.get(name)
    if k is None:
        k = canon(name)
        if len(_CANON_CACHE) < 512:
            _CANON_CACHE[name] = k
    return k


def rates_for(model):
    """返回 (输入, 缓存命中, 输出, 来源)；未收录返回 FALLBACK。"""
    k = _canon_cached(model)
    if not k:
        return None
    hit = _RATE_CACHE.get(k)
    if hit is not None:
        return hit
    r = RATES.get(k)
    if not r:
        r = FALLBACK
        for key in _KEYS_BY_LEN:
            if k.startswith(key):
                r = RATES[key]
                break
    if len(_RATE_CACHE) < 512:
        _RATE_CACHE[k] = r
    return r


def cost_split(model, pin, hit, pout):
    """把一次调用的成本拆成三段：(未命中输入, 缓存命中, 输出)，单位元。

    分开算是为了回答「钱花在哪儿」：缓存读便宜但量极大时照样是主要成本，
    只给一个总数会让人误以为「命中率 96%，没什么可优化的」。
    """
    r = rates_for(model)
    if not r:
        return None
    pin, hit, pout = int(pin or 0), int(hit or 0), int(pout or 0)
    hit = max(0, min(hit, pin))
    fresh = pin - hit
    return (fresh * r[0] / 1e6, hit * r[1] / 1e6, pout * r[2] / 1e6)


def cost_cny(model, pin, hit, pout):
    """一次调用的估算成本（元）。model 为空返回 None，其余一律给数（含 fallback）。"""
    parts = cost_split(model, pin, hit, pout)
    return None if parts is None else parts[0] + parts[1] + parts[2]


def price_src(model):
    """价格来源：list=官方刊例价 / approx=沿用上一代的近似档 / fallback=未收录。"""
    r = rates_for(model)
    if not r:
        return "none"
    if r is FALLBACK:
        return "fallback"
    return "approx" if "近似" in r[3] else "list"


if __name__ == "__main__":
    cases = [
        ("Hy4", 1_000_000, 0, 0, 6.0),
        ("Hy4", 1_000_000, 1_000_000, 0, 0.3),
        ("Hy4 preview", 0, 0, 1_000_000, 18.0),
        ("GLM-5.3", 1_000_000, 0, 0, 8.0),
        ("GLM-5.3-Flash", 1_000_000, 500_000, 1_000_000, 0.8 * 0.5 + 0.23 * 0.5 + 2.8),
        ("gpt-6-luna", 1_000_000, 0, 0, 0.667),
        ("gpt-6.1-sol", 1_000_000, 1_000_000, 0, 0.667),
        ("DeepSeek-V4.1-Flash", 1_000_000, 0, 0, 1.0),
        ("Ling-3.2-flash", 0, 0, 1_000_000, 6.003),
        ("Step-5-Preview", 1_000_000, 0, 0, 4.0),
    ]
    for model, pin, hit, pout, want in cases:
        got = cost_cny(model, pin, hit, pout)
        ok = abs(got - want) < 1e-6
        print(f"{'PASS' if ok else 'FAIL'}  {model:20s} ¥{got:.4f} 期望 ¥{want:.4f} "
              f"src={price_src(model)}")
        assert ok, model
    assert price_src("glm-5.3") == "list"
    assert price_src("glm-5.2") == "approx"
    assert price_src("Step-5-Preview") == "fallback"
    print("pricing 自测全过")
