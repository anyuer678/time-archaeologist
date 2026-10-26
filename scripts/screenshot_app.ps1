param(
    [string]$WindowTitle = "时光考古机 Time Archaeologist",
    [string]$OutFile = "shots\app.png"
)
# 按标题查找窗口并截取该窗口区域（CopyFromScreen）
Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class Win32 {
  [DllImport("user32.dll", CharSet = CharSet.Unicode)]
  public static extern IntPtr FindWindow(string cls, string title);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)]
  public static extern IntPtr FindWindowLike(string cls, string titlePart);
  [StructLayout(LayoutKind.Sequential)]
  public struct RECT { public int L; public int T; public int R; public int B; }
  [DllImport("user32.dll")]
  public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")]
  public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")]
  public static extern bool ShowWindow(IntPtr h, int cmd);
}
"@

$h = [IntPtr]::Zero
# 精确匹配失败则枚举顶层窗口做包含匹配
$h = [Win32]::FindWindow($null, $WindowTitle)
if ($h -eq [IntPtr]::Zero) {
  Get-Process | Where-Object { $_.MainWindowTitle -like "*$WindowTitle*" } | ForEach-Object {
    if ($h -eq [IntPtr]::Zero) { $h = $_.MainWindowHandle }
  }
}
if ($h -eq [IntPtr]::Zero) { Write-Error "window not found: $WindowTitle"; exit 1 }

[Win32]::ShowWindow($h, 9) | Out-Null   # SW_RESTORE
[Win32]::SetForegroundWindow($h) | Out-Null
Start-Sleep -Milliseconds 600

$r = New-Object Win32+RECT
[Win32]::GetWindowRect($h, [ref]$r) | Out-Null
$w = $r.R - $r.L; $ht = $r.B - $r.T
if ($w -le 0 -or $ht -le 0) { Write-Error "bad rect"; exit 1 }

$bmp = New-Object System.Drawing.Bitmap($w, $ht)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($r.L, $r.T, 0, 0, $bmp.Size)
$dir = Split-Path $OutFile -Parent
if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }
$bmp.Save((Resolve-Path -Path ".").Path + "\" + $OutFile, [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $bmp.Dispose()
Write-Output "saved $OutFile (${w}x${ht})"
