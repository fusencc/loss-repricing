$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $projectDir '.venv\Scripts\python.exe'
$pidFile = Join-Path $projectDir 'runtime\local-processes.json'
if (-not (Test-Path -LiteralPath $pidFile)) { Write-Output '没有本项目的进程记录。'; exit 0 }
foreach ($record in (Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json)) {
    $process = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $record.id) -ErrorAction SilentlyContinue
    if ($process -and $process.ExecutablePath -eq $pythonExe -and $process.CommandLine -like ('*' + $record.marker + '*')) {
        Stop-Process -Id $record.id -ErrorAction SilentlyContinue
        Write-Output ('已停止本项目进程 ' + $record.id)
    }
}
Remove-Item -LiteralPath $pidFile
