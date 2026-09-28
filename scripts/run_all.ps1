<#
.SYNOPSIS
    一键运行：fetch -> extract -> export -> 生成成果看板（Windows / PowerShell 版）

.DESCRIPTION
    与 scripts/run_all.sh 等价，供 Windows 上直接调用（不必装 WSL 或 Git Bash）。
    解释器固定为项目内的 .venv\Scripts\python.exe。

.EXAMPLE
    .\scripts\run_all.ps1
        跑完整流程（用 config\config.yaml 里的分页范围）

.EXAMPLE
    .\scripts\run_all.ps1 -Stages extract,export
        只重解析 + 导出（不联网，秒级）

.EXAMPLE
    .\scripts\run_all.ps1 -PageStart 1 -PageEnd 2
        覆盖分页范围，只采集第 1~2 页

.EXAMPLE
    执行策略不允许时：
    powershell -ExecutionPolicy Bypass -File .\scripts\run_all.ps1 -Stages extract,export
#>
[CmdletBinding()]
param(
    # 逗号分隔的阶段列表，可选：fetch / extract / export
    [string]$Stages = "fetch,extract,export",

    # 起始页码（覆盖 config.yaml 的 portal.page_start）
    [int]$PageStart = 0,

    # 结束页码（覆盖 config.yaml 的 portal.page_end，0 = 自动翻到最后一页）
    [int]$PageEnd = 0,

    # 跳过最后的看板生成
    [switch]$SkipReport
)

$ErrorActionPreference = "Stop"

# 项目根 = 本脚本所在目录的上一级
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"

if (-not (Test-Path $Python)) {
    Write-Host "[FAIL] 找不到解释器 $Python" -ForegroundColor Red
    Write-Host "       请先执行："
    Write-Host "         python -m venv .venv"
    Write-Host "         .\.venv\Scripts\python.exe -m pip install -r requirements.txt"
    exit 1
}

# 退出码含义（与 src/pipeline/run.py 一致）
$ExitCodes = @{
    0   = "成功"
    1   = "阶段有错误"
    2   = "参数错误"
    3   = "配置错误"
    4   = "登录态无效"
    130 = "用户中断"
}

$Extra = @()
if ($PageStart -gt 0) { $Extra += @("--page-start", "$PageStart") }
if ($PageEnd -gt 0) { $Extra += @("--page-end", "$PageEnd") }

Push-Location $Root
try {
    Write-Host "项目根  ：$Root"
    Write-Host "解释器  ：$Python"
    & $Python -c "import sys; print('Python  ：', sys.version.split()[0])"

    foreach ($Stage in ($Stages -split ",")) {
        $Stage = $Stage.Trim()
        if (-not $Stage) { continue }

        Write-Host ""
        Write-Host ("==================== 阶段：{0} ====================" -f $Stage) -ForegroundColor Cyan
        $Started = Get-Date

        $argv = @("-m", "src.pipeline.run", "--stage", $Stage) + $Extra
        & $Python @argv
        $Code = $LASTEXITCODE

        $Elapsed = (Get-Date) - $Started
        $Meaning = if ($ExitCodes.ContainsKey($Code)) { $ExitCodes[$Code] } else { "未知" }
        Write-Host ("  退出码={0}（{1}）耗时 {2:n1}s" -f $Code, $Meaning, $Elapsed.TotalSeconds)

        if ($Code -ne 0) {
            Write-Host ""
            Write-Host ("[STOP] 阶段 {0} 未成功（退出码 {1}），已终止。" -f $Stage, $Code) -ForegroundColor Red
            exit $Code
        }
    }

    if (-not $SkipReport) {
        Write-Host ""
        Write-Host "==================== 生成成果看板 ====================" -ForegroundColor Cyan
        & $Python (Join-Path $Root "scripts\make_report.py")
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }

    Write-Host ""
    Write-Host "全部完成。打开看板：" -ForegroundColor Green
    Write-Host ("  {0}" -f (Join-Path $Root "data\processed\report.html"))
    Write-Host ""
    Write-Host "检索服务（类百度页面）：" -ForegroundColor Green
    Write-Host ("  & '{0}' -m src.search.server --open" -f $Python)
}
finally {
    Pop-Location
}
