' voice-ime silent launcher (Windows Script Host / VBScript).
' Prefer the packaged EXE (Release\MiaoYu.exe); otherwise run app.py with
' pythonw.exe from PATH (no console window).
' Usage: double-click this file, or: wscript.exe run_voiceime.vbs
' NOTE: this file is kept ASCII-only on purpose: WSH reads non-BOM scripts with
' the system ANSI code page, so non-ASCII characters in code/strings are unsafe.
Option Explicit

Dim fso, shell, scriptDir, exePath, appPath, command, runErr

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
exePath = fso.BuildPath(fso.BuildPath(scriptDir, "Release"), "MiaoYu.exe")
appPath = fso.BuildPath(scriptDir, "app.py")

' 1) packaged EXE first (no local Python required)
If fso.FileExists(exePath) Then
    shell.CurrentDirectory = fso.GetParentFolderName(exePath)
    command = """" & exePath & """"
    shell.Run command, 0, False
    WScript.Quit 0
End If

' 2) fallback: pythonw.exe from PATH runs app.py
If Not fso.FileExists(appPath) Then
    MsgBox "voice-ime: neither Release\MiaoYu.exe nor app.py was found in:" & vbCrLf & scriptDir, 16, "voice-ime"
    WScript.Quit 1
End If

shell.CurrentDirectory = scriptDir
command = "pythonw.exe """ & appPath & """"
On Error Resume Next
shell.Run command, 0, False
runErr = Err.Number
On Error GoTo 0
If runErr <> 0 Then
    MsgBox "voice-ime: pythonw.exe was not found." & vbCrLf & "Install Python and add it to PATH, or build the EXE first: build\build_exe.ps1", 16, "voice-ime"
    WScript.Quit 1
End If
WScript.Quit 0
