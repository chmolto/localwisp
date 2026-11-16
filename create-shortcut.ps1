# Creates a "LocalWisp" shortcut on the Desktop that points to this folder's run.bat
$here     = $PSScriptRoot
$desktop  = [Environment]::GetFolderPath("Desktop")
$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut((Join-Path $desktop "LocalWisp.lnk"))
$Shortcut.TargetPath       = Join-Path $here "run.bat"
$Shortcut.WorkingDirectory = $here
$Shortcut.Description      = "LocalWisp - local voice dictation"
$Shortcut.IconLocation     = (Join-Path $here "assets\localwisp.ico") + ",0"
$Shortcut.WindowStyle      = 7   # start minimized: no console flash
$Shortcut.Save()

Write-Host "Shortcut created on the Desktop."
