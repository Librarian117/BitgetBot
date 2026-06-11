---
name: agent-verifier
description: Agent 代码安全审计 — 检测无限循环、Agent死锁、幻觉工具调用、Prompt爆炸、Hardcoded密钥等Agent项目特有的风险模式
status: stable
---

# Agent Verifier — 量化交易 Agent 安全审计

针对 DeepSeekQuantBot 这类 AI Agent 项目的专项审计，覆盖 Agent 特有的风险模式。

## 审计维度

### 1. 无限循环检测

检查所有 `while True` 和递归调用是否有明确退出条件：

```bash
# 找出所有 while True 循环
grep -n "while True" deepseek_quant_bot.py

# 找出所有递归调用
grep -rn "def.*\(.*self\." --include="*.py" | grep -v "super()"
```

**必须检查的模式：**
- `while True` 是否都有 `break`/`return`/异常退出
- 主循环是否有最大重试次数限制
- API 重试是否带退避策略（exponential backoff）
- 扫描循环中是否有 `time.sleep` 防止 CPU 空转

**关注文件：** `deepseek_quant_bot.py` 主扫描循环、`genetic_evolver.py` 进化迭代、`trailing_sl.py` 追踪更新

### 2. Agent 死锁检测

检查 Agent 间调用是否可能形成循环等待：

```bash
# 检查 Agent 间互相调用
grep -rn "DeepSeekAnalyst\|SignalScorer\|TradeExecutor\|RiskMonitor" --include="*.py" | grep "import\|from"
```

**死锁模式：**
- A 等 B 的锁，B 等 A 的锁（循环依赖）
- `queue.Queue().get()` 没有 timeout
- `asyncio.Event().wait()` 没有 timeout
- `threading.Lock()` 没有超时释放
- 主线程等子线程 `join()` 没有 timeout

**针对本项目：**
- 检查 `DeepSeekAnalyst` API 调用是否有超时设置（`timeout=` 参数）
- 检查 `TradeExecutor` 下单是否等待交易所返回
- 检查 `HealthMonitor` 全链路检查是否可能阻塞扫描

### 3. 幻觉工具调用检测

检查是否存在未定义的方法调用、错误的参数签名：

```bash
# 检查 ccxt API 方法调用是否正确
grep -rn "exchange\." --include="*.py" | grep -v "\.pyc"

# 检查可能打错的方法名（常见幻觉）
grep -rn "fetch_positions\|fetch_position\|create_order\|place_order\|set_leverage\|set_margin" --include="*.py"
```

**常见 LLM 幻觉模式：**
- ccxt 方法名拼错（如 `fetch_open_positions` 不存在，是 `fetch_positions`）
- 参数签名错误（如 Bitget hedge 模式不支持 `reduceOnly`）
- 凭空捏造的属性访问（如 `position.info.stopLossPrice` 实际是 `position.info['stopLoss']`）
- v4.1 已知陷阱：`fetch_open_orders(stop=True)` 查不到 pos-tpsl 止损单

**验证方法：**
- 对比 `CLAUDE.md` 中的 gotcha 列表
- 对比 ccxt 文档确认方法签名
- 实际运行 `exchange.describe()` 查看真实 API

### 4. Prompt 爆炸检测

检查 AI prompt 长度是否超出 token 限制：

```bash
# 检查 prompt 拼接逻辑
grep -rn "prompt.*=.*f\"\|prompt.*=.*+=" --include="*.py"
grep -rn "token\|len.*prompt" --include="*.py"
```

**爆炸风险点：**
- `DeepSeekAnalyst._merge_content_reasoning()` 拼接历史数据
- 多时间框架 K线数据放入 prompt（`1h` + `4h` + `15m` = 数百条数据点）
- 持仓列表全量序列化到 prompt
- 智慧库（30条）全部附加到 prompt
- 信号审核时附加全部过滤诊断信息

**防护措施：**
- 每个 prompt 拼接处应有 `len(prompt)` 限制
- 历史数据截断（最近 N 根 K线）
- Wisdom 条目限制（top 5 而非全 30）
- 预估 token 数（1 token ≈ 4 字符中文，≈ 1 字符英文）

### 5. Hardcoded 密钥检测

```bash
grep -rn "sk-\|api_key\|apiKey\|secret\|passphrase\|token.*=.*['\"]" --include="*.py" | grep -v "os\.environ\|os\.getenv\|load_dotenv\|\.env\|ConfigManager"
```

**除了标准密钥，还需检查：**
- DeepSeek API Key (`DEEPSEEK_API_KEY`)
- Bitget 凭证（已用 `.env`，检查是否泄漏到日志）
- Webhook URL（如钉钉/飞书通知）
- SSH 私钥路径

**日志泄漏检查：**
```bash
grep -rn "api_key\|secret\|passphrase\|token" logs/ --include="*.jsonl" 2>/dev/null
```

## 快速审计流程

```
1. 无限循环 → grep "while True" | 逐一检查退出条件
2. 死锁检测 → grep "timeout" | 确认所有 wait/get/join 有超时
3. 幻觉调用 → 对比 CLAUDE.md gotcha + 运行 describe()
4. Prompt 爆炸 → grep "prompt" | 检查 len() 限制
5. 硬编码密钥 → grep "sk-\|api_key\|secret" | 排除 .env
```

## 输出格式

审计结束后生成报告：

```
## Agent 安全审计报告
### 无限循环 (N 处)
- [file:line] 描述 | 风险等级 | 建议

### 死锁风险 (N 处)
- [file:line] 描述 | 风险等级 | 建议

### 幻觉调用 (N 处)
- [file:line] 描述 | 风险等级 | 建议

### Prompt 爆炸风险 (N 处)
- [file:line] 当前最大 tokens | 建议上限

### 硬编码密钥 (N 处)
- [file:line] 密钥类型 | 建议
```

## 相关文件

- [deepseek_quant_bot.py](deepseek_quant_bot.py) — 主循环 + Agent 编排
- [deepseek_analyst.py](deepseek_analyst.py) — AI 调用层
- [safety_manager.py](safety_manager.py) — API 熔断
- [health_monitor.py](health_monitor.py) — 健康检查
- [CLAUDE.md](CLAUDE.md) — gotcha 参考列表
