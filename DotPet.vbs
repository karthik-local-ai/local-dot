' Desktop pet with voice. Needs pythonw.exe on PATH (normal Windows Python install).
' Add --listen to the arguments below once dictation is configured in config.json.
Option Explicit
Dim shell, fs, folder
Set shell = CreateObject("WScript.Shell")
Set fs = CreateObject("Scripting.FileSystemObject")
folder = fs.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = folder
shell.Run "pythonw.exe " & Chr(34) & folder & "\pet.py" & Chr(34) & " --voice", 0, False
