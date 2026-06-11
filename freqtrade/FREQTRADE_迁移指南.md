# Freqtrade 迁移指南 — DeepSeekQuantBot → Freqtrade

## 调研结论

### Bitget 支持情况 (官方确认)

| 特性 | 支持 | 备注 |
|------|------|------|
| 现货 | ✅ | market/limit 止损 |
| **U本位合约** | ✅ | **Isolated only** |
| Cross 保证金 | ❌ | Bitget 在 Freqtrade 中只支持 isolated |
| **Hedge 模式** | ❌ | Freqtrade 启动时强制设为 One-way Mode |
| 止损 on exchange | ✅ | market/limit 均支持 |
| time_in_force | ✅ | GTC/FOK/IOC/PO |
| 沙箱/Demo | ❌ | Freqtrade 无 Bitget 沙箱概念，用 dry-run 替代 |

### Hedge Mode → One-way Mode 影响分析

**现状**: 你的 dashboard 显示 3 个仓位全是 LONG (做多3 做空0)，方向失衡警告。
**结论**: Hedge 模式对你目前的交易风格不是必需的。One-way 模式更简洁：
  - 每个 pair 任一时刻只能有一个方向（long/short/flat）
  - 自动避免"同币种双向持仓互相绞杀"问题
  - 策略逻辑不变，只是不能同时持多空

**建议**: 接受 One-way 模式，这是 Freqtrade 主流方式。

---

## 文件结构

```
freqtrade/
├── config.json                          # Freqtrade 主配置
├── FREQTRADE_迁移指南.md                 # 本文件
├── setup.ps1                            # Windows 一键安装脚本
├── strategies/
│   └── bitget_hybrid_strategy.py        # 6 策略合一 (可 Hyperopt 优化)
└── user_data/                           # Freqtrade 数据目录
    ├── data/                            # OHLCV 数据 (下载后)
    ├── logs/                            # 运行日志
    └── strategies/                      # 策略副本
```

---

## 安装步骤

### 1. 安装 Freqtrade

```powershell
# 推荐: 创建虚拟环境
python -m venv .venv
.venv\Scripts\activate

# 安装
pip install freqtrade

# 验证
freqtrade --version
```

### 2. 初始化用户数据目录

```powershell
cd e:\BitgetBot\freqtrade
freqtrade create-userdir --userdir user_data
```

### 3. 配置 API 密钥

编辑 `config.json`，填入 Bitget API 凭证：

```json
"exchange": {
    "name": "bitget",
    "key": "bg_xxxxxxxx",
    "secret": "xxxxxxxx",
    "password": "your_passphrase"
}
```

> ⚠️ **安全提醒**: 不要在 config.json 中硬编码密钥。生产环境建议用环境变量:
> ```powershell
> $env:FREQTRADE__EXCHANGE__KEY = "bg_xxx"
> $env:FREQTRADE__EXCHANGE__SECRET = "xxx"
> $env:FREQTRADE__EXCHANGE__PASSWORD = "xxx"
> ```

### 4. 下载历史数据

```powershell
freqtrade download-data \
  --exchange bitget \
  -t 5m 1h \
  --timerange 20240101- \
  -c config.json
```

### 5. 回测

```powershell
# 基础回测
freqtrade backtesting \
  -c config.json \
  -s BitgetHybridStrategy \
  --timerange 20240601-20260601

# 带佣金率 (Bitget taker 0.06%)
freqtrade backtesting \
  -c config.json \
  -s BitgetHybridStrategy \
  --timerange 20240601-20260601 \
  --fee 0.0006

# 导出详细交易记录
freqtrade backtesting \
  -c config.json \
  -s BitgetHybridStrategy \
  --timerange 20240601-20260601 \
  --fee 0.0006 \
  --export trades \
  --export-filename bt_result.json
```

### 6. Dry-run (模拟交易)

```powershell
freqtrade trade \
  -c config.json \
  -s BitgetHybridStrategy \
  --dry-run
```

### 7. 实盘

```powershell
freqtrade trade \
  -c config.json \
  -s BitgetHybridStrategy
```

---

## 策略对比: 原 Bot vs Freqtrade

| 维度 | DeepSeekQuantBot | BitgetHybridStrategy (Freqtrade) |
|------|-----------------|----------------------------------|
| **pullback** | EMA9 + RSI 阈值 + ADX | ✅ 完全移植, 方向过滤 |
| **momentum** | ADX + 动量RSI区 + 突破N bar | ✅ 完全移植 |
| **ema_cross** | EMA9×EMA21 + EMA200确认 | ✅ 完全移植 |
| **bollinger** | 布林上下轨 + RSI + 宽度过滤 | ✅ 完全移植 |
| **counter_trend** | 深度超卖/超买逆势 + 半仓 | ✅ 半仓 (custom_stake_amount) |
| **grid** | GridManager 订单网格 | ✅ 低波动布林带入场 (简化) |
| **7时段** | SessionManager 波动模型 | ⚠️ 仅周末保护 (custom_entry_signal) |
| **Kalman** | 方向确认 + 冲突处理 | ❌ 需自行实现指标 |
| **Hurst** | 趋势/回归分类 | ❌ 需自行实现指标 |
| **评分引擎** | 9维度加权 → AUTO/AI/REJECT | ❌ Freqtrade 二进制 buy/sell |
| **AI 审核** | DeepSeek 解析信号 | ❌ Freqtrade 无 AI 层 |
| **自学习** | SelfLearner + 遗传进化 | ⚠️ Hyperopt 可替代参数优化 |
| **风控** | 10层保护 | ✅ 止损/追踪止损/ROI/custom_exit |
| **TPSL** | 手写 pos-tpsl 映射 | ✅ 框架层统一处理 |
| **回测** | 手写反事实回测 | ✅ 内置向量化回测 + Hyperopt |

---

## 关键差异和注意事项

### 1. 无 AI 层
Freqtrade 没有 DeepSeek AI 审核。但这反而是好事:
- 你的 bot 日志里 AI 审核全是乱码 `"����ʧ��-Ĭ��ͨ��"`，实际没用
- Freqtrade 用确定性规则 + Hyperopt 超参优化替代 AI

### 2. 无 Kalman/Hurst
这些是自定义量化特征，Freqtrade 没有内置。如果确实需要:
- 可以写一个 Freqtrade 自定义指标函数
- 或者把这些维度融入入场条件（例如用 Hurst 指数过滤趋势/均值回归策略）

### 3. 无 Hedge 模式
Bitget Futures 在 Freqtrade 中是 One-way。影响:
- 不能同时持有 BTC LONG + BTC SHORT
- 如果想换方向，需要先平仓再反向开

### 4. 沙箱 vs Dry-run
原 bot 用 Bitget 沙箱 API（量数据不可靠）。Freqtrade 的 dry-run:
- 使用真实行情数据
- 在本地模拟交易
- 更准确的模拟结果

### 5. Hyperopt 替代遗传进化
原 bot 的 GeneticEvolver 可以用 Freqtrade 的 Hyperopt 替代:
```powershell
freqtrade hyperopt \
  -c config.json \
  -s BitgetHybridStrategy \
  --hyperopt-loss SharpeHyperOptLoss \
  --spaces buy sell \
  --epochs 100
```

---

## 建议的迁移路径

```
Week 1: 安装 Freqtrade → 下载数据 → 回测 6 个月
Week 2: 对比回测结果 vs 原 bot 实盘绩效 → 调整策略参数
Week 3: Dry-run 模拟交易 (至少 1 周)
Week 4: 小资金实盘 (stake_amount=10) → 逐步放大
```

---

## 故障排查

### "Bitget requires password"
```json
"exchange": {
    "password": "your_bitget_passphrase"  // 这不是登录密码, 是 API 的 passphrase
}
```

### "Bitget position mode must be One-way"
Freqtrade 启动时会自动调用 Bitget API 设置。如果失败:
- 登录 Bitget 网页端 → 合约设置 → 持仓模式 → 单向模式

### 数据下载失败
```powershell
# Bitget 一次最多返回 1000 根 K 线, 需分批下载
freqtrade download-data -t 5m --timerange 20240101-20240301 -c config.json
freqtrade download-data -t 5m --timerange 20240301-20240601 -c config.json
```
