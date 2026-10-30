import Foundation
import AppKit
import ApplicationServices
func emit(_ object:[String:Any]) {print(String(data:try! JSONSerialization.data(withJSONObject:object,options:[.sortedKeys]),encoding:.utf8)!)}
let args=CommandLine.arguments
func value(_ flag:String)->String? {guard let i=args.firstIndex(of:flag),args.count>i+1 else{return nil};return args[i+1]}
let client=value("--platform") ?? ""
let bundle=client=="wechat" ? "com.tencent.xinWeChat":client=="qq" ? "com.tencent.qq":""
guard !bundle.isEmpty else{emit(["ok":false,"status":"受限","reason":"unsupported_client"]);exit(2)}
if args.contains("--preflight") {emit(["ok":false,"status":"受限","reason":AXIsProcessTrusted() ? "explicit_window_required":"accessibility_permission_missing","accessibility_trusted":AXIsProcessTrusted(),"changed":false]);exit(0)}
guard AXIsProcessTrusted() else{emit(["ok":false,"status":"受限","reason":"accessibility_permission_missing","changed":false]);exit(0)}
guard let raw=value("--window-id"),let id=UInt32(raw),id>0,let xs=value("--x"),let ys=value("--y"),let ws=value("--width"),let hs=value("--height"),let x=Double(xs),let y=Double(ys),let width=Double(ws),let height=Double(hs),x.isFinite,y.isFinite,width.isFinite,height.isFinite,width>=200,height>=150,width<=10000,height<=10000 else{emit(["ok":false,"status":"受限","reason":"invalid_window_or_bounds","changed":false]);exit(2)}
let infos=CGWindowListCopyWindowInfo(.optionAll,kCGNullWindowID) as? [[String:Any]] ?? []
guard let info=infos.first(where:{($0[kCGWindowNumber as String] as? UInt32)==id}),let pid=info[kCGWindowOwnerPID as String] as? Int32,let app=NSRunningApplication(processIdentifier:pid),app.bundleIdentifier==bundle,let cgBounds=info[kCGWindowBounds as String] as? [String:Any],let original=CGRect(dictionaryRepresentation:cgBounds as CFDictionary) else{emit(["ok":false,"status":"受限","reason":"window_not_owned_by_selected_client","changed":false]);exit(0)}
func attr(_ e:AXUIElement,_ key:String)->CFTypeRef?{var v:CFTypeRef?;return AXUIElementCopyAttributeValue(e,key as CFString,&v) == .success ? v:nil}
func rect(_ e:AXUIElement)->CGRect? {
    guard let p=attr(e,kAXPositionAttribute),let s=attr(e,kAXSizeAttribute),CFGetTypeID(p)==AXValueGetTypeID(),CFGetTypeID(s)==AXValueGetTypeID() else{return nil}
    var position=CGPoint.zero;var size=CGSize.zero
    guard AXValueGetValue(p as! AXValue,.cgPoint,&position),AXValueGetValue(s as! AXValue,.cgSize,&size) else{return nil}
    return CGRect(origin:position,size:size)
}
let axapp=AXUIElementCreateApplication(pid)
let windows=attr(axapp,kAXWindowsAttribute) as? [AXUIElement] ?? []
let matches=windows.filter{w in guard let r=rect(w) else{return false};return abs(r.minX-original.minX)<2 && abs(r.minY-original.minY)<2 && abs(r.width-original.width)<2 && abs(r.height-original.height)<2}
guard matches.count==1 else{emit(["ok":false,"status":"受限","reason":"window_mapping_not_unique","changed":false]);exit(0)}
let target=matches[0]
var posSet:DarwinBoolean=false;var sizeSet:DarwinBoolean=false
AXUIElementIsAttributeSettable(target,kAXPositionAttribute as CFString,&posSet);AXUIElementIsAttributeSettable(target,kAXSizeAttribute as CFString,&sizeSet)
guard posSet.boolValue && sizeSet.boolValue else{emit(["ok":false,"status":"受限","reason":"window_position_or_size_not_settable","changed":false]);exit(0)}
var point=CGPoint(x:x,y:y);var size=CGSize(width:width,height:height)
let p=AXValueCreate(.cgPoint,&point)!;let s=AXValueCreate(.cgSize,&size)!
let first=AXUIElementSetAttributeValue(target,kAXSizeAttribute as CFString,s)
let second=AXUIElementSetAttributeValue(target,kAXPositionAttribute as CFString,p)
let ok=first == .success && second == .success
if ok {_ = AXUIElementPerformAction(target,kAXRaiseAction as CFString);app.activate(options:[])}
else {
    var oldPoint=original.origin;var oldSize=original.size
    _=AXUIElementSetAttributeValue(target,kAXSizeAttribute as CFString,AXValueCreate(.cgSize,&oldSize)!)
    _=AXUIElementSetAttributeValue(target,kAXPositionAttribute as CFString,AXValueCreate(.cgPoint,&oldPoint)!)
}
func encoded(_ r:CGRect)->[String:Double]{return ["x":r.minX,"y":r.minY,"width":r.width,"height":r.height]}
let actual=rect(target) ?? original
emit(["ok":ok,"status":ok ? "真实验证通过":"受限","verification_scope":"window_position_only_not_message_capture","reason":ok ? "positioned_native_client_window_not_embedded":"window_move_failed_rollback_attempted","window_id":id,"platform":client,"original_bounds":encoded(original),"new_bounds":encoded(actual),"changed":ok,"chat_content_read":false,"messages_sent":false])
