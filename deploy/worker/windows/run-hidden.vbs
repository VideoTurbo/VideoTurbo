' Starts the Worker supervisor without showing a console window.
' Registered as the action of the "VideoTurbo-Worker" scheduled task by worker.ps1.
Option Explicit

Dim shell, fileSystem, scriptPath, command
Set shell = CreateObject("WScript.Shell")
Set fileSystem = CreateObject("Scripting.FileSystemObject")
scriptPath = fileSystem.BuildPath(fileSystem.GetParentFolderName(WScript.ScriptFullName), "worker.ps1")
command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & scriptPath & """ __supervise"
shell.Run command, 0, True
