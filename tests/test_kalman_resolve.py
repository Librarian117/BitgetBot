#!/usr/bin/env python3
"""
test_kalman_resolve.py — _resolve_kalman() + get_required_score() 回归测试 v4.5

覆盖:
  - 弱/强 signal-Kalman 冲突 (检查1)
  - 弱/强 EMA-Kalman 分歧 (检查2)
  - 噪声忽略 (score ≤ 0.2)
  - kalman_dir="flat" 无冲突
  - goes_against_ema=False 同向不干预
  - 两处冲突同时存在 → 检查1 优先
  - get_required_score 策略感知门槛

运行: python tests/test_kalman_resolve.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from deepseek_quant_bot import DeepSeekQuantBot
from signal_scorer import SignalScorer


# ════════════════════════════════════════════
# 测试框架
# ════════════════════════════════════════════

FAILURES = 0
PASSES = 0


def check(case: str, actual, expected, detail: str = ""):
    global FAILURES, PASSES
    ok = actual == expected
    if ok:
        PASSES += 1
    else:
        FAILURES += 1
        print(f"  ❌ {case}: 期望={expected!r}  实际={actual!r}  {detail}")


def check_approx(case: str, actual, expected, tolerance: int = 1, detail: str = ""):
    """允许 ±tolerance 的整数近似比较 (Kalman 扣分有 int() 取整差异)"""
    global FAILURES, PASSES
    ok = abs(actual - expected) <= tolerance
    if ok:
        PASSES += 1
    else:
        FAILURES += 1
        print(f"  ❌ {case}: 期望≈{expected}  实际={actual}  {detail}")


def run(description: str):
    print(f"\n{'─'*60}")
    print(f"  {description}")
    print(f"{'─'*60}")


# ════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════

def call(direction, strategy, kalman_dir, kalman_score,
         ema_kalman_conflict, is_bearish, is_bullish):
    """调用 _resolve_kalman，返回简化 dict"""
    r = DeepSeekQuantBot._resolve_kalman(
        direction=direction,
        strategy=strategy,
        kalman_dir=kalman_dir,
        kalman_score=kalman_score,
        ema_kalman_conflict=ema_kalman_conflict,
        is_bearish_trend=is_bearish,
        is_bullish_trend=is_bullish,
    )
    return {
        "action": r["action"],
        "new_strategy": r["new_strategy"],
        "penalty": r["confidence_penalty"],
        "scorer_flag": r["kalman_conflict_for_scorer"],
    }


def required(strategy, kalman_conflict=False):
    return SignalScorer.get_required_score(strategy, kalman_conflict)


# ════════════════════════════════════════════
# 检查1: signal-Kalman 方向冲突
# ════════════════════════════════════════════

def test_signal_kalman_conflict():
    """signal-Kalman 冲突: SHORT vs kalman=up, LONG vs kalman=down"""
    run("检查1: signal-Kalman 方向冲突分级")

    # ── 强冲突 score > 0.8 ──
    r = call("SHORT", "pullback", "up", 0.9,
             False, True, False)
    check("强冲突-action",     r["action"], "downgrade")
    check("强冲突-strategy",   r["new_strategy"], "counter_trend")
    check_approx("强冲突-penalty", r["penalty"], 22)       # int(0.9*25)=22
    check("强冲突-scorer",     r["scorer_flag"], True)

    # ── 中冲突 0.5 < score ≤ 0.8 ──
    r = call("LONG", "momentum", "down", 0.7,
             False, True, False)
    check("中冲突-action",     r["action"], "downgrade")
    check("中冲突-strategy",   r["new_strategy"], "counter_trend")
    check_approx("中冲突-penalty", r["penalty"], 14)       # int(0.7*20)=14
    check("中冲突-scorer",     r["scorer_flag"], True)

    # ── 中冲突边界 score=0.51 ──
    r = call("SHORT", "pullback", "up", 0.51,
             False, False, True)
    check("中冲突边界-action",   r["action"], "downgrade")
    check("中冲突边界-strategy", r["new_strategy"], "counter_trend")
    check_approx("中冲突边界-penalty", r["penalty"], 10)   # int(0.51*20)=10
    check("中冲突边界-scorer",   r["scorer_flag"], True)

    # ── 弱冲突 0.2 < score ≤ 0.5 ──
    r = call("LONG", "ema_cross", "down", 0.4,
             False, True, False)
    check("弱冲突-action",     r["action"], "penalize")
    check("弱冲突-strategy",   r["new_strategy"], None)     # 不改策略
    check_approx("弱冲突-penalty", r["penalty"], 10)        # int(0.4*25)=10
    check("弱冲突-scorer",     r["scorer_flag"], False)     # 不提门槛

    # ── 弱冲突边界 score=0.21 ──
    r = call("SHORT", "pullback", "up", 0.21,
             False, False, True)
    check("弱冲突边界-action",   r["action"], "penalize")
    check("弱冲突边界-strategy", r["new_strategy"], None)
    check_approx("弱冲突边界-penalty", r["penalty"], 5)     # int(0.21*25)=5
    check("弱冲突边界-scorer",   r["scorer_flag"], False)

    # ── 噪声 score ≤ 0.2 ──
    r = call("SHORT", "pullback", "up", 0.15,
             False, True, False)
    check("噪声-action",       r["action"], "allow")
    check("噪声-penalty",      r["penalty"], 0)
    check("噪声-scorer",       r["scorer_flag"], False)

    # ── 噪声边界 score=0.20 ──
    r = call("LONG", "pullback", "down", 0.20,
             False, True, False)
    check("噪声边界-action",   r["action"], "allow")
    check("噪声边界-penalty",  r["penalty"], 0)
    check("噪声边界-scorer",   r["scorer_flag"], False)


# ════════════════════════════════════════════
# 检查2: EMA-Kalman 分歧
# ════════════════════════════════════════════

def test_ema_kalman_conflict():
    """EMA-Kalman 分歧: 信号逆EMA趋势 + Kalman与EMA方向不同"""
    run("检查2: EMA-Kalman 分歧 (无 signal-Kalman 冲突时)")

    # ── 强Kalman + pullback 逆EMA → downgrade + 扣分 + 提门槛 ──
    # 场景: 熊市(LONG逆EMA), Kalman UP score=0.8, pullback LONG
    r = call("LONG", "pullback", "flat", 0.8,   # flat→检查1跳过
             True, True, False)                   # EMA熊, 非牛
    check("强EMA-pullback-action",   r["action"], "downgrade")
    check("强EMA-pullback-strategy", r["new_strategy"], "counter_trend")
    check_approx("强EMA-pullback-penalty", r["penalty"], 16)  # min(0.8*20=16,20)
    check("强EMA-pullback-scorer",   r["scorer_flag"], True)

    # ── 强Kalman + 非pullback 逆EMA → penalize + 扣分 ──
    r = call("LONG", "momentum", "flat", 0.7,
             True, True, False)
    check("强EMA-momentum-action",   r["action"], "penalize")
    check("强EMA-momentum-strategy", r["new_strategy"], None)     # 不改策略
    check_approx("强EMA-momentum-penalty", r["penalty"], 14)      # min(0.7*20=14,20)
    check("强EMA-momentum-scorer",   r["scorer_flag"], False)     # 不提门槛

    # ── 弱Kalman + pullback 逆EMA → 仅降级，不扣分不提门槛 ──
    r = call("SHORT", "pullback", "flat", 0.3,
             True, False, True)     # 牛市(SHORT逆EMA), Kalman=down, score=0.3≤0.5
    check("弱EMA-pullback-action",   r["action"], "downgrade")
    check("弱EMA-pullback-strategy", r["new_strategy"], "counter_trend")
    check("弱EMA-pullback-penalty",  r["penalty"], 0)             # 不扣分!
    check("弱EMA-pullback-scorer",   r["scorer_flag"], False)     # 不提门槛!

    # ── 弱Kalman + 非pullback 逆EMA → allow (不干预) ──
    r = call("LONG", "ema_cross", "flat", 0.3,
             True, True, False)
    check("弱EMA-emacross-action",   r["action"], "allow")
    check("弱EMA-emacross-penalty",  r["penalty"], 0)
    check("弱EMA-emacross-scorer",   r["scorer_flag"], False)

    # ── Kalman强上限: penalty capped at 20 ──
    r = call("LONG", "pullback", "flat", 1.5,
             True, True, False)
    check("cap-penalty", r["penalty"], 20)  # min(int(1.5*20)=30, 20) = 20

    # ── goes_against_ema=False 不可达测试移除 ──
    # ema_kalman_conflict=True 需要 kalman_dir∈{up,down}, 此时检查1必命中
    # goes_against_ema=False 分支仅在 kalman_dir="flat" 时可达, 但
    # ema_kalman_conflict 的定义决定了 kalman_dir≠"flat" 时它才为 True
    # → 保留该分支为防御代码, 不单独测试

    # ── ema_kalman_conflict=False → 检查2跳过, 返回 allow ──
    r = call("LONG", "pullback", "flat", 0.9,
             False, True, False)    # 无EMA-Kalman冲突
    check("无EMA冲突-action",  r["action"], "allow")
    check("无EMA冲突-penalty", r["penalty"], 0)


# ════════════════════════════════════════════
# 两个冲突同时存在 → 检查1优先
# ════════════════════════════════════════════

def test_both_conflicts():
    """检查1优先: signal-Kalman 冲突先于 EMA-Kalman 分歧"""
    run("优先级: signal-Kalman 冲突 > EMA-Kalman 分歧")

    # signal-Kalman 强冲突 (SHORT vs up, score=0.9)
    # 同时 EMA-Kalman conflict (熊市+Kalman=up, LONG逆EMA)
    # → 检查1 先命中，返回 downgrade + counter_trend + 扣22
    r = call("SHORT", "pullback", "up", 0.9,
             True, True, False)     # 两种冲突同时存在
    check("双重-action",     r["action"], "downgrade")
    check("双重-strategy",   r["new_strategy"], "counter_trend")
    check_approx("双重-penalty", r["penalty"], 22)  # 来自检查1: 0.9*25=22
    check("双重-scorer",     r["scorer_flag"], True)

    # 对比: 如果只有 EMA 冲突 (无 signal 冲突), pullback+强Kalman
    # 应该是 downgrade + 扣 min(0.9*20,20)=18
    r2 = call("LONG", "pullback", "flat", 0.9,    # flat→跳过检查1
              True, True, False)
    check("仅EMA-action",    r2["action"], "downgrade")
    check_approx("仅EMA-penalty", r2["penalty"], 18)  # 来自检查2: min(0.9*20=18,20)


# ════════════════════════════════════════════
# kalman_dir="flat" → 无 signal 冲突
# ════════════════════════════════════════════

def test_kalman_flat():
    """kalman_dir='flat' → 检查1永远跳过"""
    run("kalman_dir='flat' 无 signal-Kalman 冲突")

    r = call("SHORT", "pullback", "flat", 0.99,
             False, True, False)
    check("flat强-action",    r["action"], "allow")
    check("flat强-penalty",   r["penalty"], 0)

    # flat + EMA冲突 = 仅走检查2
    r = call("LONG", "pullback", "flat", 0.6,
             True, True, False)
    check("flat+EMA-action",  r["action"], "downgrade")
    check("flat+EMA-strategy", r["new_strategy"], "counter_trend")


# ════════════════════════════════════════════
# get_required_score 策略感知门槛
# ════════════════════════════════════════════

def test_required_score():
    """get_required_score 策略感知"""
    run("get_required_score 策略感知门槛")

    check("pullback",          required("pullback"), 50)
    check("momentum",          required("momentum"), 50)
    check("ema_cross",         required("ema_cross"), 50)
    check("bollinger",         required("bollinger"), 50)
    check("grid",              required("grid"), 50)
    check("counter_trend",     required("counter_trend"), 58)
    check("ct+kalman",         required("counter_trend", True), 65)
    check("ct+kalman=False",   required("counter_trend", False), 58)
    # non-counter_trend kalman_conflict 不影响门槛
    check("pullback+kalman",   required("pullback", True), 50)
    check("momentum+kalman",   required("momentum", True), 50)


# ════════════════════════════════════════════
# 综合场景: 端到端 kalman_action → required_score
# ════════════════════════════════════════════

def test_end_to_end():
    """端到端: kalman_action + 下游 required_score"""
    run("端到端: _resolve_kalman → get_required_score")

    scenarios = [
        # (desc, direction, strategy, kalman_dir, score, ema_conflict,
        #  is_bear, is_bull,
        #  exp_action, exp_strat, exp_penalty, exp_scorer_flag, exp_req)

        # ── signal-Kalman 冲突 ──
        ("强冲突>0.8", "SHORT", "pullback", "up", 0.9, False, True, False,
         "downgrade", "counter_trend", 22, True, 65),

        ("强冲突>0.8-2", "LONG", "momentum", "down", 0.85, False, False, True,
         "downgrade", "counter_trend", 21, True, 65),

        ("中冲突>0.5", "SHORT", "ema_cross", "up", 0.6, False, True, False,
         "downgrade", "counter_trend", 12, True, 65),

        ("弱冲突>0.2", "LONG", "pullback", "down", 0.35, False, False, True,
         "penalize", None, 8, False, 50),

        ("噪声≤0.2", "SHORT", "pullback", "up", 0.1, False, True, False,
         "allow", None, 0, False, 50),

        # ── EMA-Kalman 分歧 (kalman_dir="flat" 跳过检查1) ──
        ("强EMA+pullback", "LONG", "pullback", "flat", 0.8, True, True, False,
         "downgrade", "counter_trend", 16, True, 65),

        ("强EMA+momentum", "LONG", "momentum", "flat", 0.7, True, True, False,
         "penalize", None, 14, False, 50),

        ("弱EMA+pullback", "SHORT", "pullback", "flat", 0.3, True, False, True,
         "downgrade", "counter_trend", 0, False, 58),

        ("弱EMA+emacross", "LONG", "ema_cross", "flat", 0.3, True, True, False,
         "allow", None, 0, False, 50),

        # ── 无冲突 ──
        ("无冲突", "SHORT", "pullback", "down", 0.9, False, True, False,
         "allow", None, 0, False, 50),

        # ── 无冲突: kalman="down" + SHORT 同向, EMA牛, 无EMA冲突 ──
        ("无冲突同向", "SHORT", "pullback", "down", 0.7, False, False, True,
         "allow", None, 0, False, 50),
    ]

    for (desc, direction, strategy, kalman_dir, score, ema_conflict,
         is_bear, is_bull,
         exp_action, exp_strat, exp_penalty, exp_scorer_flag, exp_req) in scenarios:

        r = call(direction, strategy, kalman_dir, score,
                 ema_conflict, is_bear, is_bull)
        final_strat = exp_strat or strategy
        final_req = required(final_strat, exp_scorer_flag)

        check(f"{desc}-action",        r["action"], exp_action, desc)
        check(f"{desc}-strategy",      r["new_strategy"], exp_strat, desc)
        check_approx(f"{desc}-penalty", r["penalty"], exp_penalty, 1, desc)
        check(f"{desc}-scorer_flag",   r["scorer_flag"], exp_scorer_flag, desc)
        check(f"{desc}-required_score", final_req, exp_req,
              f"strategy={final_strat} kalman={exp_scorer_flag}")


# ════════════════════════════════════════════
# Main
# ════════════════════════════════════════════

if __name__ == "__main__":
    test_signal_kalman_conflict()
    test_ema_kalman_conflict()
    test_both_conflicts()
    test_kalman_flat()
    test_required_score()
    test_end_to_end()

    total = PASSES + FAILURES
    print(f"\n{'='*60}")
    print(f"  结果: {PASSES}/{total} 通过", end="")
    if FAILURES > 0:
        print(f"  ❌ {FAILURES} 失败")
        sys.exit(1)
    else:
        print(f"  ✅ 全部通过")
    print(f"{'='*60}")
