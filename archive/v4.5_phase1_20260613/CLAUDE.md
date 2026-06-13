# DeepSeekQuantBot v4.5

Bitget 沙箱 U本位合约 | 每5分钟扫描 | 量化决策 + AI研究层 | 6策略 | 10层风控

**阅读路径**: `docs/README.md` → `docs/architecture/overview.md` → `docs/design/strategy-design.md` → `AGENTS.md`

## 规则

- 全程中文 | 优先模块化/异步/风控 | 禁止未来函数 | 类型提示+dataclass+完整异常
- 每个信号证据只消费一次 (v4.5 核心原则)
- AI 不是决策者 — `DEEPSEEK_MASTER_SWITCH` 总开关控制

## 启动/部署

```bash
# 本地
python deepseek_quant_bot.py

# 部署 (Git → Push → Server)
git push origin main
ssh root@8.210.3.197 "cd /root/BitgetBot && git pull && bash deploy.sh"
```

## 关键 Gotcha

- `fetch_open_orders(stop=True)` 查不到 pos-tpsl | Hedge 不支持 `reduceOnly`
- 1h/4h K线用 `iloc[-2]` | 沙箱量数据不可靠
