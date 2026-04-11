# Crea un acceso directo en el Escritorio para WispLocal
$WshShell   = New-Object -ComObject WScript.Shell
$Shortcut   = $WshShell.CreateShortcut("$env:USERPROFILE\Desktop\WispLocal.lnk")
$Shortcut.TargetPath       = "$env:USERPROFILE\Documents\wisp\lanzar.bat"
$Shortcut.WorkingDirectory = "$env:USERPROFILE\Documents\wisp"
$Shortcut.Description      = "WispLocal - Dictado por voz"
$Shortcut.IconLocation     = "shell32.dll,168"   # icono de micrófono de Windows
$Shortcut.Save()

Write-Host "Acceso directo creado en el Escritorio."
