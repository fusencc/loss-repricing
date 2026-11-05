$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $projectDir '.venv\Scripts\python.exe'
$pidFile = Join-Path $projectDir 'runtime\local-processes.json'
if (-not (Test-Path -LiteralPath $pidFile)) { Write-Output '没有本项目的进程记录。'; exit 0 }
foreach ($record in (Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json)) {
    $process = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $record.id) -ErrorAction SilentlyContinue
    if ($process -and $process.ExecutablePath -eq $pythonExe -and $process.CommandLine -like ('*' + $record.marker + '*')) {
        # Windows虚拟环境python.exe会启动基础解释器子进程；仅停启动器会遗留监听端口。
        # 上面已核对本项目可执行路径及命令，随后仅结束该已确认进程树。
        & taskkill.exe /PID $record.id /T /F | Out-Null
        if ($LASTEXITCODE -ne 0) { throw ('无法停止本项目进程树 ' + $record.id) }
        Write-Output ('已停止本项目进程 ' + $record.id)
    }
}
Remove-Item -LiteralPath $pidFile
