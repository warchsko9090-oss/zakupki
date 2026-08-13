# Creates desktop shortcut for Zakupki launcher.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$desktop = [Environment]::GetFolderPath("Desktop")
$lnkPath = Join-Path $desktop ([string]([char]0x0417 + [char]0x0430 + [char]0x043A + [char]0x0443 + [char]0x043F + [char]0x043A + [char]0x0438) + ".lnk")
$target = Join-Path $root "launch-desktop.vbs"
$icon = Join-Path $root "assets\zakupki.ico"

if (-not (Test-Path $target)) { throw "launch-desktop.vbs not found" }
if (-not (Test-Path $icon)) { throw "assets\zakupki.ico not found" }

$w = New-Object -ComObject WScript.Shell
$s = $w.CreateShortcut($lnkPath)
$s.TargetPath = $target
$s.WorkingDirectory = $root
$s.WindowStyle = 7
$s.Description = "Zakupki - restart server and open Overview"
$s.IconLocation = "$icon,0"
$s.Save()

Write-Host "Shortcut created:"
Write-Host $lnkPath
