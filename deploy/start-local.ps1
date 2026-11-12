param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectDir
$pythonExe = Join-Path $projectDir '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) {
    python -m venv (Join-Path $projectDir '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Python虚拟环境创建失败' }
    & $pythonExe -m pip install -r (Join-Path $projectDir 'backend\requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw '依赖安装失败' }
}
New-Item -ItemType Directory -Path (Join-Path $projectDir 'runtime') -Force | Out-Null
if (-not (Test-Path -LiteralPath (Join-Path $projectDir '.env'))) {
    & $pythonExe -c "from pathlib import Path; import secrets; p=Path('.env'); p.write_text('LR_ENVIRONMENT=development\nLR_ADAPTER_MODE=demo\nLR_ADMIN_USERNAME=admin\nLR_ADMIN_PASSWORD='+secrets.token_urlsafe(18)+'\nLR_SESSION_SECRET='+secrets.token_urlsafe(40)+'\nLR_LIVE_SUBMIT_ENABLED=false\n',encoding='utf-8')"
    if ($LASTEXITCODE -ne 0) { throw '本地配置创建失败' }
}
if (-not (Test-Path -LiteralPath (Join-Path $projectDir 'frontend\dist\index.html'))) {
    Push-Location -LiteralPath (Join-Path $projectDir 'frontend')
    try { npm.cmd ci; if ($LASTEXITCODE -ne 0) { throw '前端依赖安装失败' }; npm.cmd run build; if ($LASTEXITCODE -ne 0) { throw '前端构建失败' } }
    finally { Pop-Location }
}
$pidFile = Join-Path $projectDir 'runtime\local-processes.json'
if (Test-Path -LiteralPath $pidFile) {
    $record = Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json
    $ownedRunning = @($record | Where-Object {
        $process = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $_.id) -ErrorAction SilentlyContinue
        $process -and $process.ExecutablePath -eq $pythonExe -and $process.CommandLine -like ('*' + $_.marker + '*')
    })
    if ($ownedRunning.Count -gt 0) {
        Write-Output '已有本项目进程运行，请先用deploy/stop-local.ps1停止后再重启。'
        exit 0
    }
}
if (Get-NetTCPConnection -LocalPort 18765 -State Listen -ErrorAction SilentlyContinue) { throw '18765端口已被占用，未启动或停止任何其他程序。' }
& $pythonExe -m alembic -c backend/alembic.ini upgrade head
if ($LASTEXITCODE -ne 0) { throw '数据库迁移失败' }
$env:PYTHONPATH = Join-Path $projectDir 'backend'
$apiProcess = Start-Process -FilePath $pythonExe -ArgumentList '-m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 18765 --no-access-log' -WorkingDirectory $projectDir -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $projectDir 'runtime\api.stdout.log') -RedirectStandardError (Join-Path $projectDir 'runtime\api.stderr.log')
$workerProcess = Start-Process -FilePath $pythonExe -ArgumentList '-m app.worker' -WorkingDirectory $projectDir -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $projectDir 'runtime\worker.stdout.log') -RedirectStandardError (Join-Path $projectDir 'runtime\worker.stderr.log')
@(@{id=$apiProcess.Id; marker='uvicorn app.main:create_app'}, @{id=$workerProcess.Id; marker='-m app.worker'}) | ConvertTo-Json | Set-Content -LiteralPath $pidFile -Encoding utf8
$ready = $false
for ($attempt=0; $attempt -lt 20; $attempt++) {
    try { $health = Invoke-RestMethod 'http://127.0.0.1:18765/api/health'; if ($health.status -eq 'ok') { $ready=$true; break } } catch { }
    Start-Sleep -Milliseconds 500
}
if (-not $ready) { throw '服务未就绪，请查看runtime日志。进程记录已保留，可运行stop-local.ps1停止。' }
Write-Output '已启动：http://127.0.0.1:18765；账号admin，密码见项目根目录.env的LR_ADMIN_PASSWORD。'
if (-not $NoBrowser) { Start-Process 'http://127.0.0.1:18765' }
