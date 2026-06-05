#!/usr/bin/env python3
"""session_manager.py — 加密货币 7 时段波动模型"""

from typing import Any, Dict

from time_utils import now


class SessionManager:
    """
    根据当前时间自动调整策略参数。
    加密货币波动性因时段/星期而异：
      - 工作日欧美重叠盘(20-24 CST): 波动最大，放宽条件加大仓位
      - 工作日亚洲盘(8-16): 正常波动
      - 工作日低活跃期(0-8): 波动小，收紧条件
      - 周末: 低量震荡，大幅放宽ADX但要缩仓
    """

    @staticmethod
    def get_session(base_adx: int = 20, base_vol: float = 1.5) -> Dict[str, Any]:
        """v3.7: 加密货币真实波动时段 — 7 时段模型 (UTC+8 / CST)。

        加密市场波动规律 (按活跃度排序):
          1. 美股重叠盘 20:00-00:00 — 美+欧重叠，峰值波动，爆仓高发
          2. 美股后半段 00:00-04:00 — 美股独导，高波动，方向性强
          3. 欧洲盘     16:00-20:00 — 欧盘主力，活跃度上升
          4. 亚洲盘     08:00-16:00 — 正常亚洲资金，波动中等
          5. 周末活跃段 14:00-02:00 — 周末也有行情，不宜全压
          6. 亚洲凌晨   04:00-08:00 — 全球真空期，最低波动
          7. 周末凌晨   02:00-14:00 — 周末最低波动

        参数含义:
          adx_threshold: 信号入场门槛 (越低越容易触发)
          margin_mult:   仓位乘数 (<1 减仓, >1 加仓)
          sl_mult:       止损宽度 (波动大→窄止损防止意外, 波动小→宽止损防止噪音)
          max_positions: 最大并发持仓数
        """
        _now = now()
        weekday = _now.weekday()
        hour = _now.hour
        is_weekend = weekday >= 5

        # ── 周末分支 ──
        if is_weekend:
            if 14 <= hour or hour < 2:
                # 周末活跃段 14:00-02:00 — 欧美散户在线
                return {"name": "weekend_active", "label": "周末活跃",
                        "adx_threshold": max(10, base_adx - 6),
                        "margin_mult": 0.7, "vol_ratio": max(0.3, base_vol * 0.2),
                        "sl_mult": 1.8, "max_positions": 4,
                        "advice": "周末活跃段-适度参与"}
            else:
                # 周末凌晨 02:00-14:00 — 全球休息
                return {"name": "weekend_dead", "label": "周末凌晨",
                        "adx_threshold": min(25, base_adx + 2),
                        "margin_mult": 0.4, "vol_ratio": max(0.5, base_vol * 0.3),
                        "sl_mult": 2.2, "max_positions": 2,
                        "advice": "周末凌晨-最小仓位"}

        # ── 工作日分支 ──
        if hour >= 20:
            # 美股重叠盘 20:00-00:00 — 波动峰值
            return {"name": "us_overlap", "label": "美股重叠",
                    "adx_threshold": max(10, base_adx - 6),
                    "margin_mult": 1.2, "vol_ratio": max(0.5, base_vol * 0.6),
                    "sl_mult": 1.2, "max_positions": 8,
                    "advice": "波动峰值-降门槛+窄止损"}
        if 0 <= hour < 4:
            # 美股后半段 00:00-04:00 — 美股主导，仍活跃
            return {"name": "us_late", "label": "美股后半",
                    "adx_threshold": max(10, base_adx - 4),
                    "margin_mult": 1.0, "vol_ratio": max(0.4, base_vol * 0.5),
                    "sl_mult": 1.3, "max_positions": 6,
                    "advice": "美股主导-方向性强"}
        if 16 <= hour < 20:
            # 欧洲盘 16:00-20:00
            return {"name": "europe", "label": "欧洲盘",
                    "adx_threshold": max(12, base_adx - 4),
                    "margin_mult": 1.1, "vol_ratio": max(0.5, base_vol * 0.6),
                    "sl_mult": 1.3, "max_positions": 6,
                    "advice": "欧洲活跃-适度放宽"}
        if 8 <= hour < 16:
            # 亚洲盘 08:00-16:00
            return {"name": "asia", "label": "亚洲盘",
                    "adx_threshold": base_adx,
                    "margin_mult": 1.0, "vol_ratio": base_vol,
                    "sl_mult": 1.5, "max_positions": 6,
                    "advice": "亚洲基准-正常参数"}
        # 亚洲凌晨 04:00-08:00 — 全球真空
        return {"name": "asia_dead", "label": "亚洲凌晨",
                "adx_threshold": min(25, base_adx + 4),
                "margin_mult": 0.5, "vol_ratio": max(0.5, base_vol * 0.4),
                "sl_mult": 1.8, "max_positions": 3,
                "advice": "全球真空-收紧+宽止损防噪音"}

    @staticmethod
    def effective_params(config) -> Dict[str, Any]:
        session = SessionManager.get_session(config.adx_threshold, config.vol_ratio_threshold)
        return {
            **session,
            "adx_threshold": session["adx_threshold"],
            "margin_min": config.adx_margin_min * session["margin_mult"],
            "margin_max": config.adx_margin_max * session["margin_mult"],
            "vol_ratio": session["vol_ratio"],
            "sl_atr_mult": config.sl_atr_mult * session["sl_mult"],
        }
