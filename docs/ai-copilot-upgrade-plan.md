# AI 副驾驶多 Agent 架构升级 — 评估与实施计划

## 背景

当前系统 (v4.1) 已完成了关键转型：**AI 从决策者降级为研究者**。量化信号直通交易，AI 只做信号解释、复盘、周期审查、规则生成、参数建议。这与 GPT 建议的核心理念一致。

现有 AI 调用分布在 3 个文件、3 个独立 HTTP Session、10+ 个调用点，通过 `getattr(config, 'flag', default)` 各自守卫。`SelfLearner` 已有初步的多 Agent 概念（strategy_analyst / risk_auditor / execution_reviewer 三路并行），但仅限于逐笔交易复盘。

## GPT 建议 vs 现状对照

| 建议 | 现状 | 差距 |
|------|------|------|
| **AI 风控审计 Agent** (30min 组合扫描) | 已有逐笔 `risk_auditor`，但无组合级定期扫描 | 需新增组合级视角 |
| **策略研究 Agent** (1000笔模式挖掘) | 已有 `periodic_review()` (4h)，但较通用 | 需增强为定向模式挖掘 |
| **异常检测 Agent** (胜率骤降检测) | 已有 `review_market_anomaly()` (市场异常)，**不是**策略绩效异常 | 这是真正的空白 |
| **AI Manager 统一入口** | `DEEPSEEK_MASTER_SWITCH` 只在 init 时覆盖子开关 | 缺运行时统一调用接口 |
| **多 Agent 独立 Prompt** | `SelfLearner` 三 Agent 已有独立 system prompt | 已部分实现 |

## 我的评估

### ✅ 强烈建议做的

**1. AI Manager 统一入口** — 优先级最高
- 当前 3 个独立的 `requests.Session`（DeepSeekAnalyst、SelfLearner、GeneticEvolver），各自管理连接池
- `getattr(config, 'flag', default)` 散落在 10+ 个方法里
- 统一后：单点断路器、单点成本追踪、单点超时配置、方便 Claude Code 后续重构
- 风险最低，不影响任何交易逻辑

**2. 策略绩效异常检测 Agent** — 真正填补空白
- 当前 `performance_tracker.py` 已有完整的分策略/分方向/分币种指标
- 已有 `get_rolling_metrics(window=20)` 可以做滚动对比
- 缺失的是：自动对比"今天 vs 历史30天"，发现显著偏离时触发 AI 分析
- 这个 Agent 做本地统计 + AI 解释，token 消耗很小

### ⚠️ 有价值但需调整的

**3. 组合级风控审计 Agent**
- GPT 建议 30 分钟一次，考虑到 API 成本，建议改为 **2 小时或事件驱动**（连续亏损触发）
- 当前 `RiskMonitor` 和 `SafetyManager` 已经做了硬风控，AI 审计是锦上添花
- 关键是"只记录不执行"——这个约束非常重要，必须 enforce

**4. 策略研究 Agent (增强 periodic_review)**
- GPT 说的"分析 1000 笔交易"不可行——1000 笔的上下文太长，API 成本极高
- 正确做法：本地预计算统计特征 → 发送摘要给 AI → AI 生成假设 → 本地验证
- 这其实是对现有 `periodic_review()` 的升级，不需要全新模块

### ❌ 不建议做的

**5. "1000笔交易全量发给 AI"**
- Token 成本过高（1000 笔 × ~200 tokens/笔 = 200K tokens 输入）
- 替代方案：本地聚合统计 + AI 只看摘要

**6. 所有 Agent 独立文件**
- GPT 建议拆成 6 个独立 Agent 文件。当前 `SelfLearner` 已经是 2000 行——再拆会导致循环引用和状态同步问题
- 建议：AI Manager 一个文件，Agent 定义可以内聚在 Manager 中，不需要 6 个文件

## 实施计划

### 阶段 1：AI Manager 基础设施 (`ai_manager.py`)

新建 `ai_manager.py`，提供：

```python
class AIManager:
    """所有 AI 调用的统一入口
    
    - 统一 MASTER_SWITCH 检查
    - 统一 HTTP Session 管理 (替换 3 个独立 Session)
    - 统一断路器 (替换 DeepSeekAnalyst 的内置断路器)
    - 统一成本追踪 (token 消耗日志)
    - 统一超时/重试
    """
    
    def enabled(self) -> bool: ...
    def call(self, agent_name: str, system_prompt: str, user_prompt: str, 
             schema: dict = None) -> dict: ...
    
    # Agent 工厂方法
    def reflection_agent(self, trade_data: dict) -> dict: ...
    def risk_auditor_agent(self, portfolio_summary: dict) -> dict: ...
    def anomaly_agent(self, strategy_stats: dict) -> dict: ...
    def research_agent(self, trade_summary: dict) -> dict: ...
```

**改动范围：**
- 新建 `ai_manager.py` (~300 行)
- `deepseek_quant_bot.py`: DeepSeekAnalyst 的 HTTP 调用改为委托给 AIManager
- `self_learner.py`: SelfLearner 的 HTTP 调用改为委托给 AIManager  
- `genetic_evolver.py`: GeneticEvolver 的 HTTP 调用改为委托给 AIManager
- `config_manager.py`: 新增 cost_tracking 相关配置

### 阶段 2：策略绩效异常检测 (`anomaly_agent.py` 功能，内聚在 AIManager)

在 AIManager 中实现 `anomaly_agent`：

1. 每 2 小时（或事件驱动：新交易平仓后），从 `performance_tracker` 获取：
   - 各策略最近 20 笔 vs 历史全部 的胜率对比
   - 各方向最近 20 笔 vs 历史全部 的胜率对比
2. 本地计算偏离度：`|recent_winrate - historical_winrate| > 20%` → 触发
3. 仅触发时调用 DeepSeek 分析原因
4. 输出：市场结构是否变化、建议权重调整（仅记录，不自动执行）

**改动范围：**
- `ai_manager.py`: 新增 `anomaly_agent()` 方法
- `deepseek_quant_bot.py`: 在主循环中增加调用点（融合到现有定期审查流程中）
- `performance_tracker.py`: 新增 `get_strategy_comparison()` 方法（当前 vs 历史对比）

### 阶段 3：组合风控审计 (AIManager 中的 `risk_auditor_agent`)

1. 触发条件：每 2 小时 OR 连续 3 笔亏损
2. 输入：当前持仓、今日交易、保证金使用率、方向分布
3. 调用 DeepSeek 输出风险评估
4. **硬约束：不修改任何仓位，只写日志**

**改动范围：**
- `ai_manager.py`: 新增 `risk_auditor_agent()` 方法
- `deepseek_quant_bot.py`: 新增 `_run_risk_audit()` 调用点

### 阶段 4：策略研究增强 (升级 periodic_review)

1. 增强 `SelfLearner.periodic_review()` 的输入数据：
   - 加入分时段胜率（亚洲/欧洲/美股）
   - 加入分策略胜率矩阵
2. AI 输出从"通用建议"升级为"可验证假设"
3. 自动将高置信度建议写入 wisdom store

**改动范围：**
- `self_learner.py`: 增强 `periodic_review()` 方法
- `performance_tracker.py`: 新增 `get_time_window_metrics()` 方法

## 关键设计原则

1. **AI 永远不直接修改仓位或参数** — 只输出建议，由现有风控层裁决
2. **DEEPSEEK_MASTER_SWITCH=false 时 100% 静默** — 零 API 调用
3. **本地统计优先** — 能本地算的就不要发给 AI
4. **向后兼容** — 所有新 Agent 默认启用，但可通过 `.env` 独立关闭
5. **不拆散 SelfLearner** — 保持其作为学习引擎的内聚性，AI Manager 只做"调用基础设施"

## 验证方式

1. `DEEPSEEK_MASTER_SWITCH=false` → 启动 bot，确认零 AI 调用
2. 正常启动 → 观察日志中 AIManager 的成本追踪输出
3. 模拟策略胜率骤降 → 确认 anomaly_agent 触发
4. 连续亏损 → 确认 risk_auditor 触发
5. 运行 `python deepseek_quant_bot.py` 完整回归
