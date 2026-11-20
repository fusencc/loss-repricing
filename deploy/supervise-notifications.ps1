# 当前 Windows 用户的通知守护；应用退出不影响，登录时自动启动。
$ErrorActionPreference='Stop'
$createdNew=$false
$notificationMutex=[System.Threading.Mutex]::new($true,'Local\LossRepricingDingTalkNotifications',[ref]$createdNew)
if(-not $createdNew){exit 0}
try {
    while($true){
        try {
            $notificationProcess=Start-Process -FilePath "$env:WINDIR\System32\wsl.exe" -ArgumentList '--distribution loss-repricing --cd /home/developer/projects/loss-repricing --exec /usr/bin/env PYTHONPATH=/home/developer/projects/loss-repricing/backend /home/developer/projects/loss-repricing/.venv/bin/python -m app.notification_worker' -WindowStyle Hidden -PassThru
            $notificationProcess.WaitForExit()
        } catch { }
        Start-Sleep -Seconds 15
    }
} finally { $notificationMutex.ReleaseMutex(); $notificationMutex.Dispose() }