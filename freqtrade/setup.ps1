# Freqtrade 一键安装脚本 (Windows)
# 用法: .\setup.ps1          # 安装并配置
#       .\setup.ps1 -Backtest  # 安装后跑回测
#       .\setup.ps1 -DryRun    # 安装后启动 dry-run

param([switch]$Backtest, [switch]$DryRun)

$ErrorActionPreference = "Stop"
$FREQTRADE_DIR = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "╔══════════════════════════════════════════════════╗" -ForegroundColor Cyan
Write-Host "║  Freqtrade Bitget 混合策略 — 一键部署             ║" -ForegroundColor Cyan
Write-Host "╚══════════════════════════════════════════════════╝" -ForegroundColor Cyan

# ── 1. 虚拟环境 ──
Write-Host "`n[1/5] 创建虚拟环境..." -ForegroundColor Yellow
if (-not (Test-Path "$FREQTRADE_DIR\.venv")) {
    python -m venv "$FREQTRADE_DIR\.venv"
    Write-Host "  ✅ .venv 创建完成" -ForegroundColor Green
} else {
    Write-Host "  ⏭️  .venv 已存在" -ForegroundColor DarkGray
}

# ── 2. 激活并安装 ──
Write-Host "`n[2/5] 安装 Freqtrade..." -ForegroundColor Yellow
& "$FREQTRADE_DIR\.venv\Scripts\activate.ps1"
pip install freqtrade -q
Write-Host "  ✅ Freqtrade 安装完成" -ForegroundColor Green

# ── 3. 验证 ──
Write-Host "`n[3/5] 验证安装..." -ForegroundColor Yellow
$ver = & "$FREQTRADE_DIR\.venv\Scripts\freqtrade.exe" --version 2>&1
Write-Host "  ✅ $ver" -ForegroundColor Green

# ── 4. 初始化用户目录 ──
Write-Host "`n[4/5] 初始化用户数据目录..." -ForegroundColor Yellow
if (-not (Test-Path "$FREQTRADE_DIR\user_data")) {
    & "$FREQTRADE_DIR\.venv\Scripts\freqtrade.exe" create-userdir --userdir "$FREQTRADE_DIR\user_data"
}
Write-Host "  ✅ user_data 就绪" -ForegroundColor Green

# ── 5. 复制策略 ──
Write-Host "`n[5/5] 部署策略文件..." -ForegroundColor Yellow
Copy-Item "$FREQTRADE_DIR\strategies\bitget_hybrid_strategy.py" "$FREQTRADE_DIR\user_data\strategies\" -Force
Write-Host "  ✅ 策略已部署" -ForegroundColor Green

Write-Host "`n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
Write-Host "  安装完成!  下一步:" -ForegroundColor Green
Write-Host "  1. 编辑 config.json 填入 Bitget API 密钥" -ForegroundColor White
Write-Host "  2. 下载数据: freqtrade download-data -t 5m --timerange 20240101-" -ForegroundColor White
Write-Host "  3. 回测验证: freqtrade backtesting -c config.json -s BitgetHybridStrategy" -ForegroundColor White
Write-Host "  4. 模拟运行: freqtrade trade -c config.json -s BitgetHybridStrategy --dry-run" -ForegroundColor White
Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan

# ── 可选: 直接回测 ──
if ($Backtest) {
    Write-Host "`n🔄 启动回测..." -ForegroundColor Yellow
    & "$FREQTRADE_DIR\.venv\Scripts\freqtrade.exe" backtesting `
        -c "$FREQTRADE_DIR\config.json" `
        -s BitgetHybridStrategy `
        --timerange 20240601-20260601 `
        --fee 0.0006
}

# ── 可选: 直接 dry-run ──
if ($DryRun) {
    Write-Host "`n🚀 启动 Dry-Run 模拟交易..." -ForegroundColor Yellow
    & "$FREQTRADE_DIR\.venv\Scripts\freqtrade.exe" trade `
        -c "$FREQTRADE_DIR\config.json" `
        -s BitgetHybridStrategy `
        --dry-run
}
