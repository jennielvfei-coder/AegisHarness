# bridge-stuck-check.ps1 — 诊断 lark-channel-bridge 死锁的 scope
param([switch]$Fix)

$logDir = "$env:USERPROFILE\.lark-channel\logs"
$today = (Get-Date).ToString("yyyy-MM-dd")
$logFile = Join-Path $logDir "$today.log"

if (-not (Test-Path $logFile)) { Write-Host "今天还没有日志"; exit 1 }

$entries = Get-Content $logFile | ForEach-Object {
    try { $_ | ConvertFrom-Json } catch {}
}

# 收集 pool acquire / release
$acquires = @{}
$releases = [System.Collections.Generic.HashSet[string]]::new()
foreach ($e in $entries) {
    if ($e.phase -eq "pool" -and $e.event -eq "acquired") {
        $acquires[$e.traceId] = @{ chat = $e.chatId; ts = $e.ts }
    }
    if ($e.phase -eq "pool" -and $e.event -eq "released") {
        [void]$releases.Add($e.traceId)
    }
}

# 找出有 acquire 但没有 release 的 scope
$deadlocked = @()
foreach ($kv in $acquires.GetEnumerator()) {
    if (-not $releases.Contains($kv.Key)) {
        # 找这个 trace 的最后一条日志
        $lastEntry = ($entries | Where-Object { $_.traceId -eq $kv.Key } | Select-Object -Last 1)
        $lastTs = if ($lastEntry) { $lastEntry.ts } else { $kv.Value.ts }
        $minutesAgo = [math]::Round((Get-Date).Subtract([datetime]$lastTs).TotalMinutes, 1)

        $deadlocked += [PSCustomObject]@{
            TraceId    = $kv.Key
            ChatId     = $kv.Value.chat
            Started    = $kv.Value.ts
            LastEvent  = $lastEntry.phase + "." + $lastEntry.event
            MinutesAgo = $minutesAgo
        }
    }
}

if ($deadlocked.Count -eq 0) {
    Write-Host "没有发现死锁的 scope。"
    exit 0
}

Write-Host "=== 发现 $($deadlocked.Count) 个死锁 scope ==="
$deadlocked | Format-Table -AutoSize

if ($Fix) {
    Write-Host "正在重启 bridge..."
    lark-channel-bridge restart
} else {
    Write-Host ""
    Write-Host "修复方法:"
    Write-Host "  1. 先试试在群里发 /stop"
    Write-Host "  2. 如果 /stop 1分钟内没反应，运行:"
    Write-Host "     pwsh D:\Claude\scripts\bridge-stuck-check.ps1 -Fix"
    Write-Host "     (这会重启 bridge，所有活跃会话都会断开)"
}
