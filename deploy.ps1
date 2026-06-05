# deploy.ps1 - DeepSeekQuantBot 一键部署
# ==========================================
# 内部调用 sync.ps1 做智能 MD5 差异化同步。
#
# 用法:
#   .\deploy.ps1                # 完整: 语法检查 → 停止 → 上传变更 → 重启 → 日志
#   .\deploy.ps1 -Quick         # 快速: 上传变更, 不重启
#   .\deploy.ps1 -DryRun        # 仅查看有无差异
#   .\deploy.ps1 -Full          # 强制全量上传
param([switch]$Quick, [switch]$DryRun, [switch]$Full)

Write-Host "=== DeepSeekQuantBot Deploy ===" -ForegroundColor Cyan

# 委托到 sync.ps1
$syncScript = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "sync.ps1"
if ($Quick)   { & $syncScript -NoRestart }
elseif ($DryRun) { & $syncScript -DryRun }
elseif ($Full) { & $syncScript -Full }
else          { & $syncScript }
