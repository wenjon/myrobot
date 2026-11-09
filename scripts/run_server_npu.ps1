# 用 Ryzen AI 隔离环境启动后端，让视觉走 ONNX + VitisAI（NPU）。
#
# 为什么需要单独脚本：
#   VitisAIExecutionProvider 来自 .venv-ryzenai 里的 onnxruntime-vitisai，
#   直接用系统 Python 启动时通常没有这个 provider，只能回退 CPU。
#
# 用法（在仓库根目录）：
#   .\scripts\run_server_npu.ps1
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Py = Join-Path $Root ".venv-ryzenai\Scripts\python.exe"

if (-not (Test-Path $Py)) {
    Write-Host "[错误] 找不到 $Py" -ForegroundColor Red
    Write-Host "请先按 docs/vision-design.md 创建 .venv-ryzenai 并安装 Ryzen AI 运行时。" -ForegroundColor Yellow
    exit 1
}

if (-not $env:RYZEN_AI_INSTALLATION_PATH) {
    $env:RYZEN_AI_INSTALLATION_PATH = "C:\Program Files\RyzenAI\1.7.0"
}

Set-Location $Root
Write-Host "[启动] Python=$Py" -ForegroundColor Cyan
Write-Host "[启动] RYZEN_AI_INSTALLATION_PATH=$env:RYZEN_AI_INSTALLATION_PATH" -ForegroundColor Cyan
& $Py "backend\server.py" @args
