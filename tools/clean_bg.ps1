param([switch]$DryRun)

$ErrorActionPreference = "SilentlyContinue"
chcp 65001 | Out-Null
[Console]::OutputEncoding = [Text.Encoding]::UTF8

function Get-FreeGB {
    Add-Type -AssemblyName Microsoft.VisualBasic
    $ci = New-Object Microsoft.VisualBasic.Devices.ComputerInfo
    return [math]::Round($ci.AvailablePhysicalMemory / 1GB, 2)
}

# 需要关闭的第三方后台（进程名，不含 .exe）
$TARGETS = @(
    # 浏览器
    "msedge", "chrome", "firefox", "360se", "360chrome", "qqbrowser",
    "msedgewebview2", "opera", "brave",
    # 聊天/社交（QQ/微信走"孤儿进程"逻辑，见 ORPHAN_GROUPS）
    "tim", "dingtalk", "discord", "kook",
    "telegram", "skype", "feishu", "lark",
    # 网盘/下载
    "onedrive", "baidunetdisk", "quark", "thunder", "weiyun", "aliyundrive",
    "115browser", "xunlei",
    # 音视频/娱乐
    "cloudmusic", "qqmusic", "spotify", "kugou", "kuwo",
    "douyin", "doubao", "bilibili", "iqiyi", "youku",
    # 游戏平台
    "steam", "steamwebhelper", "wegame", "epicgameslauncher", "battle.net",
    # 工具/更新器（360 本体保留，只清绑定软件）
    "360se", "360chrome", "360softmgr", "360drvmgr", "360zip", "360game",
    "360docprotect", "360pic", "360widget", "360downloads", "360qgame",
    "qqpcmgr", "huorong",
    "googleupdate", "microsoftedgeupdate", "adobeupdater",
    "jusched", "ccleaner", "wpscloudsvr", "wpscenter",
    # MuMu 远程服务（能杀就杀）
    "mumuremotebackend", "mumuremoteservice", "mumuremotehealthd"
)

# ── 孤儿进程组：本体还在就不动，本体没了才清理残留 ──
$ORPHAN_GROUPS = @(
    @{
        label   = "QQ"
        main    = @("qq")
        members = @("qqex", "qqprotect", "txplatform", "qqpcrtx", "qqpcmm")
    },
    @{
        label   = "微信"
        main    = @("wechat", "weixin")
        members = @("wechatappex", "wechatocr", "wechatutility", "wechatplayer",
                    "wechatbrowser", "wechatstore", "wechatweb", "wxwork", "wxworkweb")
    }
)

# 绝对不能碰的（系统 + 我们自己的 + 正在用的开发工具）
$KEEP = @(
    "system", "registry", "smss", "csrss", "wininit", "winlogon", "services",
    "lsass", "svchost", "dwm", "explorer", "fontdrvhost", "sihost", "ctfmon",
    "taskhostw", "runtimebroker", "searchhost", "startmenuexperiencehost",
    "shellexperiencehost", "applicationframehost", "textinputhost",
    "securityhealthsystray", "msmpeng", "audiodg", "conhost", "dllhost",
    "backgroundtaskhost", "lockapp", "python", "pythonw", "ollama",
    "llama-server", "node", "powershell", "pwsh", "cmd", "windowsterminal",
    "pycharm64", "pycharm", "code", "idea64", "devenv", "mingw32",
    "mumunxmain", "mumunxdevice", "mumunxservice", "mumunxsvc",
    "msedge_proxy", "textinputhost",
    # 360 安全卫士本体（保留，只清绑定软件）
    "360safe", "360tray", "360sd", "360rp", "360realpro", "360leakfixer"
)

Write-Host ""
Write-Host "  ================================================" -ForegroundColor Cyan
Write-Host "         🐟  一键清理第三方后台  🐟" -ForegroundColor Cyan
Write-Host "  ================================================" -ForegroundColor Cyan
Write-Host ""

$before = Get-FreeGB
Write-Host ("  当前可用内存: " + $before + " GB") -ForegroundColor Gray
Write-Host ""

# 扫描
$found = @()
$seen = @{}
foreach ($p in (Get-Process | Where-Object { $_.WorkingSet64 -gt 30MB })) {
    $n = $p.ProcessName.ToLower()
    if ($KEEP -contains $n) { continue }
    if ($TARGETS -notcontains $n) { continue }
    if ($seen[$n]) {
        $seen[$n].Count++
        $seen[$n].MB += [math]::Round($p.WorkingSet64 / 1MB)
        continue
    }
    $item = [PSCustomObject]@{
        Name  = $n
        PID   = $p.Id
        MB    = [math]::Round($p.WorkingSet64 / 1MB)
        Count = 1
    }
    $seen[$n] = $item
    $found += $item
}

# ── 孤儿进程：QQ / 微信 本体在就跳过，本体没了才清残留 ──
$orphanNotes = @()
foreach ($g in $ORPHAN_GROUPS) {
    $mainAlive = $false
    foreach ($m in $g.main) {
        if (Get-Process -Name $m -ErrorAction SilentlyContinue) { $mainAlive = $true; break }
    }
    foreach ($mem in $g.members) {
        $procs = @(Get-Process -Name $mem -ErrorAction SilentlyContinue)
        if ($procs.Count -eq 0) { continue }
        $mb = [math]::Round((($procs | Measure-Object WorkingSet64 -Sum).Sum) / 1MB)
        if ($mainAlive) {
            $orphanNotes += ("{0} 本体在运行，保留其后台（{1} {2}MB）" -f $g.label, $mem, $mb)
            continue
        }
        if ($seen.ContainsKey($mem)) { continue }
        $item = [PSCustomObject]@{
            Name  = $mem
            PID   = $procs[0].Id
            MB    = $mb
            Count = $procs.Count
        }
        $seen[$mem] = $item
        $found += $item
        $orphanNotes += ("{0} 本体已退出，清理残留（{1} {2}MB）" -f $g.label, $mem, $mb)
    }
}

if ($found.Count -eq 0) {
    Write-Host "  ✅ 没有发现需要清理的第三方后台" -ForegroundColor Green
    if ($orphanNotes.Count -gt 0) {
        Write-Host ""
        foreach ($n in $orphanNotes) { Write-Host ("    · " + $n) -ForegroundColor DarkGray }
    }
    Write-Host ""
    Write-Host "  按任意键退出…" -ForegroundColor Gray
    $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
    exit 0
}

if ($orphanNotes.Count -gt 0) {
    Write-Host "  QQ / 微信 处理：" -ForegroundColor DarkCyan
    foreach ($n in $orphanNotes) { Write-Host ("    · " + $n) -ForegroundColor DarkGray }
    Write-Host ""
}

Write-Host "  将关闭以下后台程序：" -ForegroundColor Yellow
Write-Host ""
$totalMB = 0
foreach ($f in ($found | Sort-Object MB -Descending)) {
    $line = "    · " + $f.Name.PadRight(20) + " " + ([string]$f.MB).PadLeft(6) + " MB"
    if ($f.Count -gt 1) { $line += "  (" + $f.Count + " 个进程)" }
    Write-Host $line -ForegroundColor White
    $totalMB += $f.MB
}
Write-Host ""
Write-Host ("  预计释放: " + [math]::Round($totalMB / 1024, 2) + " GB") -ForegroundColor Cyan
Write-Host ""
Write-Host "  系统进程、本控制台、开发工具都不会动。" -ForegroundColor DarkGray
Write-Host ""

if ($DryRun) {
    Write-Host "  [演练模式] 不实际执行" -ForegroundColor Magenta
    exit 0
}

Write-Host "  3 秒后开始清理，按任意键取消…" -ForegroundColor Yellow -NoNewline
$cancelled = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Milliseconds 100
    if ($Host.UI.RawUI.KeyAvailable) {
        $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
        $cancelled = $true
        break
    }
}
Write-Host ""
if ($cancelled) {
    Write-Host "  已取消，什么都没动。" -ForegroundColor Gray
    exit 0
}

# 执行清理
$killed = 0
$failed = 0
foreach ($f in $found) {
    $procs = Get-Process -Name $f.Name -ErrorAction SilentlyContinue
    foreach ($p in $procs) {
        $n = $p.ProcessName.ToLower()
        if ($KEEP -contains $n) { continue }
        try {
            Stop-Process -Id $p.Id -Force -ErrorAction Stop
            $killed++
        } catch {
            $failed++
        }
    }
}

Start-Sleep -Seconds 2
$after = Get-FreeGB
$delta = [math]::Round($after - $before, 2)

Write-Host ""
Write-Host "  ================================================" -ForegroundColor Cyan
Write-Host ("   ✅ 已关闭 " + $killed + " 个进程") -ForegroundColor Green
if ($failed -gt 0) {
    Write-Host ("   ⚠ " + $failed + " 个需要管理员权限，可右键本文件「以管理员身份运行」") -ForegroundColor Yellow
}
Write-Host ("   内存: " + $before + " GB  →  " + $after + " GB   释放 " + $delta + " GB") -ForegroundColor Green
Write-Host "  ================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  按任意键退出…" -ForegroundColor Gray
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
