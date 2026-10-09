<#
.SYNOPSIS
  Capture the top-level Chrome window(s) of one browser process into a JPEG (PrintWindow with
  PW_RENDERFULLCONTENT, so it also works for windows that are off-screen or covered).

.DESCRIPTION
  Used by run_probe_matrix.py to look for browser-UI surfaces a page cannot see (the
  "controlled by automated test software" infobar, the "unsupported command-line flag" infobar,
  first-run / search-engine-choice dialogs). Prints one JSON line describing every visible
  Chrome_WidgetWin_1 window of the process and which one was captured.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File winshot.ps1 -ProcessId 1234 -Out shot.jpg
#>
param(
  [Parameter(Mandatory = $true)][int]$ProcessId,
  [Parameter(Mandatory = $true)][string]$Out,
  [int]$MaxWidth = 1100,
  [int]$Quality = 55
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
Add-Type -TypeDefinition @"
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
public static class PPWin {
  public delegate bool EnumProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr l);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetClassName(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr hdc, uint flags);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  public static List<IntPtr> Find(uint pid) {
    var found = new List<IntPtr>();
    EnumWindows((h, x) => {
      uint p; GetWindowThreadProcessId(h, out p);
      if (p == pid && IsWindowVisible(h)) {
        var sb = new StringBuilder(256); GetClassName(h, sb, 256);
        if (sb.ToString() == "Chrome_WidgetWin_1") found.Add(h);
      }
      return true;
    }, IntPtr.Zero);
    return found;
  }
  public static string Title(IntPtr h) { var sb = new StringBuilder(512); GetWindowText(h, sb, 512); return sb.ToString(); }
}
"@

$windows = @()
$best = $null; $bestArea = -1
foreach ($h in [PPWin]::Find([uint32]$ProcessId)) {
  $r = New-Object PPWin+RECT
  [void][PPWin]::GetWindowRect($h, [ref]$r)
  $w = $r.R - $r.L; $hh = $r.B - $r.T
  $info = [ordered]@{ hwnd = [int64]$h; title = [PPWin]::Title($h); left = $r.L; top = $r.T; width = $w; height = $hh;
                      minimized = [PPWin]::IsIconic($h); foreground = ([PPWin]::GetForegroundWindow() -eq $h) }
  $windows += $info
  if ($w * $hh -gt $bestArea -and $info.title) { $best = $h; $bestArea = $w * $hh; $bestInfo = $info }
}
$result = [ordered]@{ windows = $windows; captured = $null; bytes = 0 }
if ($best) {
  $w = $bestInfo.width; $hh = $bestInfo.height
  $bmp = New-Object System.Drawing.Bitmap $w, $hh
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $hdc = $g.GetHdc()
  $ok = [PPWin]::PrintWindow($best, $hdc, 2)
  $g.ReleaseHdc($hdc); $g.Dispose()
  $scale = [Math]::Min(1.0, $MaxWidth / [double]$w)
  $nw = [int]($w * $scale); $nh = [int]($hh * $scale)
  $small = New-Object System.Drawing.Bitmap $nw, $nh
  $g2 = [System.Drawing.Graphics]::FromImage($small)
  $g2.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
  $g2.DrawImage($bmp, 0, 0, $nw, $nh); $g2.Dispose(); $bmp.Dispose()
  $codec = [System.Drawing.Imaging.ImageCodecInfo]::GetImageEncoders() | Where-Object { $_.MimeType -eq 'image/jpeg' }
  $ep = New-Object System.Drawing.Imaging.EncoderParameters 1
  $ep.Param[0] = New-Object System.Drawing.Imaging.EncoderParameter ([System.Drawing.Imaging.Encoder]::Quality), ([int64]$Quality)
  $small.Save($Out, $codec, $ep); $small.Dispose()
  $result.captured = [ordered]@{ hwnd = $bestInfo.hwnd; printWindowOk = $ok; file = $Out; scaledTo = "${nw}x${nh}" }
  $result.bytes = (Get-Item $Out).Length
}
$result | ConvertTo-Json -Compress -Depth 5
