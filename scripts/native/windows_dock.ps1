param([ValidateSet('wechat','qq')][string]$Platform,[long]$WindowHandle,[int]$X,[int]$Y,[int]$Width,[int]$Height)
$ErrorActionPreference='Stop'
Add-Type @'
using System;
using System.Runtime.InteropServices;
public class ChatLensDock {
 [StructLayout(LayoutKind.Sequential)] public struct RECT {public int Left,Top,Right,Bottom;}
 [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h,out uint p);
 [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h,out RECT r);
 [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr h);
 [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h,IntPtr after,int x,int y,int cx,int cy,uint flags);
}
'@
function Result($value){$value|ConvertTo-Json -Depth 5 -Compress}
$handle=[IntPtr]$WindowHandle
if($Width -lt 200 -or $Height -lt 150 -or $Width -gt 10000 -or $Height -gt 10000 -or -not [ChatLensDock]::IsWindow($handle)){Result @{ok=$false;status='受限';reason='invalid_window_or_bounds';changed=$false};exit 0}
[uint32]$ownerPid=0
[void][ChatLensDock]::GetWindowThreadProcessId($handle,[ref]$ownerPid)
$process=Get-Process -Id $ownerPid -ErrorAction SilentlyContinue
$allowed=if($Platform -eq 'qq'){@('QQ')}else{@('WeChat','Weixin')}
if($null -eq $process -or $allowed -notcontains $process.ProcessName){Result @{ok=$false;status='受限';reason='window_not_owned_by_selected_client';changed=$false};exit 0}
$before=New-Object ChatLensDock+RECT
if(-not [ChatLensDock]::GetWindowRect($handle,[ref]$before)){Result @{ok=$false;status='受限';reason='window_bounds_unavailable';changed=$false};exit 0}
$ok=[ChatLensDock]::SetWindowPos($handle,[IntPtr]::Zero,$X,$Y,$Width,$Height,0x0040)
$after=New-Object ChatLensDock+RECT
[void][ChatLensDock]::GetWindowRect($handle,[ref]$after)
Result @{ok=$ok;status=$(if($ok){'真实验证通过'}else{'受限'});reason=$(if($ok){'positioned_native_client_window_not_embedded'}else{'window_move_failed'});platform=$Platform;window_id=$WindowHandle;original_bounds=@{x=$before.Left;y=$before.Top;width=$before.Right-$before.Left;height=$before.Bottom-$before.Top};new_bounds=@{x=$after.Left;y=$after.Top;width=$after.Right-$after.Left;height=$after.Bottom-$after.Top};verification_scope='window_position_only_not_message_capture';changed=$ok;chat_content_read=$false;messages_sent=$false}
