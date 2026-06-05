# sync.ps1 - DeepSeekQuantBot 智能同步部署
# 用法: .\sync.ps1 [-DryRun] [-NoRestart] [-Full]
param([switch]$DryRun, [switch]$NoRestart, [switch]$Full)

$S = "8.210.3.197"
$U = "root"
$R = "/root/BitgetBot"
$L = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host ""
Write-Host "=== DeepSeekQuantBot Sync ===" -ForegroundColor Cyan

# Step 1: 本地文件 + MD5
Write-Host "[1/3] Local scan..." -ForegroundColor Yellow
$local = @{}
Get-ChildItem -Path $L -Filter "*.py" -File | Where-Object {
    $_.Name -notin @("check_orders.py","check_pos_tpsl.py","clean_orders.py","test_tpsl.py")
} | ForEach-Object {
    $local[$_.Name] = @{
        Path = $_.FullName
        MD5  = (Get-FileHash $_.FullName -Algorithm MD5).Hash
    }
}
Write-Host "  Local: $($local.Count) .py files" -ForegroundColor Gray

# Step 2: 服务器文件 + MD5 (remote_md5.sh 已存在于服务器)
Write-Host "[2/3] Remote scan + MD5..." -ForegroundColor Yellow
# 先上传辅助脚本 (如果变更)
scp -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 -q "$L\remote_md5.sh" ${U}@${S}:/tmp/
scp -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 -q "$L\start.sh" ${U}@${S}:${R}/

$remoteMD5 = @{}
$raw = ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 ${U}@${S} "bash /tmp/remote_md5.sh"
if ($raw) {
    foreach ($line in ($raw -split '\n')) {
        $line = $line.Trim()
        if ($line -match '^(\S+\.py)\s+([a-f0-9]{32})$') {
            $remoteMD5[$Matches[1]] = $Matches[2]
        }
    }
}
Write-Host "  Remote: $($remoteMD5.Count) .py files" -ForegroundColor Gray

# Step 3: 对比 + 同步
Write-Host "[3/3] Diff & sync..." -ForegroundColor Yellow
$up = @()
$same = 0

if ($Full) {
    foreach ($k in $local.Keys) { $up += $local[$k] }
    Write-Host "  FULL mode" -ForegroundColor Yellow
} else {
    foreach ($k in $local.Keys) {
        $lMD5 = $local[$k].MD5
        $rMD5 = $remoteMD5[$k]
        if (-not $rMD5) {
            $up += $local[$k]
            Write-Host "  NEW $k" -ForegroundColor Yellow
        } elseif ($lMD5 -eq $rMD5) {
            $same++
        } else {
            $up += $local[$k]
            Write-Host "  DIF $k" -ForegroundColor Red
        }
    }
}

Write-Host "  --- $same same, $($up.Count) changed ---"
Write-Host ""

if ($up.Count -eq 0) {
    Write-Host "ALL SYNCED" -ForegroundColor Green
    exit 0
}
if ($DryRun) {
    Write-Host "DRYRUN - $($up.Count) files would be uploaded" -ForegroundColor Cyan
    exit 0
}

# Stop bot
if (-not $NoRestart) {
    Write-Host "Stop bot..." -ForegroundColor Yellow
    ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 ${U}@${S} "pkill -9 -f deepseek_quant_bot.py 2>/dev/null; echo stopped"
    Start-Sleep -Seconds 2
}

# Upload changed files
Write-Host "Upload $($up.Count) files..." -ForegroundColor Yellow
$good = 0
foreach ($f in $up) {
    scp -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 -q $($f.Path) ${U}@${S}:${R}/
    if ($?) {
        Write-Host "  OK  $(Split-Path -Leaf $f.Path)" -ForegroundColor Green
        $good++
    } else {
        Write-Host "  FAIL $(Split-Path -Leaf $f.Path)" -ForegroundColor Red
    }
}
Write-Host "  Done: $good files" -ForegroundColor Gray

# Restart bot
if (-not $NoRestart) {
    Write-Host "Start bot..." -ForegroundColor Yellow
    ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 ${U}@${S} "bash $R/start.sh"
    Start-Sleep -Seconds 3
    Write-Host "  -- Recent logs --" -ForegroundColor DarkGray
    ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 ${U}@${S} "tail -5 /tmp/bot.log"
}

Write-Host ""
Write-Host "=== DONE: $good files synced ===" -ForegroundColor Green
