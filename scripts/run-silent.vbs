' Runs run-production.cmd with NO visible window (Task Scheduler points
' here, not at the .cmd directly - schtasks/Task Scheduler running a .cmd
' pops a console window even when the task itself is hidden).
' Window style 0 = hidden; True = wait for completion (so Task Scheduler's
' own overlap/history tracking reflects the real run duration).
Set sh = CreateObject("WScript.Shell")
scriptDir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
sh.Run """" & scriptDir & "\run-production.cmd""", 0, True
