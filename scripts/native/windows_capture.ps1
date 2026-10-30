param([Parameter(Mandatory=$true)][long]$WindowHandle,[Parameter(Mandatory=$true)][string]$Output,[string]$Crop='0.25,0,0.75,0.85')
$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Drawing
Add-Type @'
using System;
using System.Runtime.InteropServices;
public class ChatLensCapture {
 [StructLayout(LayoutKind.Sequential)] public struct RECT {public int Left,Top,Right,Bottom;}
 [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h,out RECT r);
 [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
 [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
 [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h,IntPtr dc,uint flags);
}
'@
$handle=[IntPtr]$WindowHandle
if([ChatLensCapture]::IsIconic($handle) -or [ChatLensCapture]::GetForegroundWindow() -ne $handle){throw 'selected_window_not_foreground'}
$rect=New-Object ChatLensCapture+RECT
if(-not [ChatLensCapture]::GetWindowRect($handle,[ref]$rect)){throw 'window_bounds_unavailable'}
$width=$rect.Right-$rect.Left;$height=$rect.Bottom-$rect.Top
if($width -lt 1 -or $height -lt 1 -or $width -gt 10000 -or $height -gt 10000){throw 'invalid_window_bounds'}
$bitmap=New-Object System.Drawing.Bitmap($width,$height)
$graphics=[System.Drawing.Graphics]::FromImage($bitmap)
$dc=$graphics.GetHdc()
try{$ok=[ChatLensCapture]::PrintWindow($handle,$dc,2)}finally{$graphics.ReleaseHdc($dc)}
try{
 if(-not $ok){throw 'window_capture_failed'}
 $region=@($Crop.Split(',')|ForEach-Object{[double]::Parse($_,[System.Globalization.CultureInfo]::InvariantCulture)})
 if($region.Length -ne 4 -or $region[0] -lt 0 -or $region[1] -lt 0 -or $region[2] -le 0 -or $region[3] -le 0 -or $region[0]+$region[2] -gt 1 -or $region[1]+$region[3] -gt 1){throw 'invalid_crop'}
 $rect=New-Object System.Drawing.Rectangle([int][Math]::Floor($width*$region[0]),[int][Math]::Floor($height*$region[1]),[int][Math]::Floor($width*$region[2]),[int][Math]::Floor($height*$region[3]))
 $clipped=$bitmap.Clone($rect,$bitmap.PixelFormat)
 try{$clipped.Save($Output,[System.Drawing.Imaging.ImageFormat]::Png)}finally{$clipped.Dispose()}
}finally{$graphics.Dispose();$bitmap.Dispose()}
@{status='captured';width=$width;height=$height;scope='selected window only; content completeness unverified'}|ConvertTo-Json -Compress
