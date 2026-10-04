' Desktop pet, typing only (no voice). Needs pythonw.exe on PATH.
Option Explicit
Dim shell, fs, folder
Set shell = CreateObject("WScript.Shell")
Set fs = CreateObject("Scripting.FileSystemObject")
folder = fs.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = folder
shell.Run "pythonw.exe " & Chr(34) & folder & "\pet.py" & Chr(34), 0, False
