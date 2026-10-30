param([ValidateSet('wechat','qq')][string]$Platform='wechat', [switch]$Capture, [long]$WindowHandle=0)
$ErrorActionPreference='Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type @'
using System;
using System.Runtime.InteropServices;
public class ChatLensWin {
 [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
 [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
 [DllImport("user32.dll")] public static extern IntPtr OpenInputDesktop(uint f,bool i,uint a);
 [DllImport("user32.dll")] public static extern bool CloseDesktop(IntPtr h);
}
'@
$names=if($Platform -eq 'qq'){@('QQ')}else{@('WeChat','Weixin')}
$procs=Get-Process -ErrorAction SilentlyContinue | Where-Object {$names -contains $_.ProcessName}
$desktop=[ChatLensWin]::OpenInputDesktop(0,$false,0x0100)
$locked=$desktop -eq [IntPtr]::Zero
if(-not $locked){[void][ChatLensWin]::CloseDesktop($desktop)}
$result=@{os='Windows';platform=$Platform;running=($null -ne $procs);screen_locked=$locked;capture_mode='uia_visible';scope='explicitly selected visible window; no history, background or multiple-conversation guarantee';windows=@()}
foreach($p in $procs){
 if($p.MainWindowHandle -eq 0){continue}
 $handle=$p.MainWindowHandle
 if($WindowHandle -ne 0 -and $handle -ne $WindowHandle){continue}
 $info=@{window_id=$handle.ToInt64();minimized=[ChatLensWin]::IsIconic($handle);frontmost=([ChatLensWin]::GetForegroundWindow() -eq $handle);version='unknown';nodes=0;text_nodes=0;text_characters=0;roles=@{}}
 try{$info.version=$p.MainModule.FileVersionInfo.ProductVersion}catch{}
 try{
  $root=[System.Windows.Automation.AutomationElement]::FromHandle($handle)
  $nodes=$root.FindAll([System.Windows.Automation.TreeScope]::Descendants,[System.Windows.Automation.Condition]::TrueCondition)
  $fragments=@()
  for($i=0;$i -lt [Math]::Min($nodes.Count,2000);$i++){
   $node=$nodes.Item($i);$role=$node.Current.ControlType.ProgrammaticName
   if(-not $info.roles.ContainsKey($role)){$info.roles[$role]=0};$info.roles[$role]++
   $info.nodes++
   if($role -eq 'ControlType.Text' -and -not $node.Current.IsOffscreen){
    $text=$node.Current.Name
    if($text){$info.text_nodes++;$info.text_characters+=$text.Length;if($Capture){$fragments+=@{text=$text;confidence=1.0}}}
   }
  }
  if($Capture){$info.fragments=$fragments}
  $info.uia_status='readable'
 }catch{$info.uia_status='restricted';$info.error_type=$_.Exception.GetType().Name}
 $result.windows+=,$info
}
$result|ConvertTo-Json -Depth 8 -Compress
