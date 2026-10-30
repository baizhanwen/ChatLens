import Foundation
import AppKit
@preconcurrency import ScreenCaptureKit
import CoreMedia
import CoreVideo
import CryptoKit
import ApplicationServices

// Metadata-only local watcher. No images, text, hashes, titles or pixels leave process.
func emit(_ object:[String:Any]) {
    var object=object
    object["observed_at"]=ISO8601DateFormatter().string(from:Date())
    if let data=try? JSONSerialization.data(withJSONObject:object,options:[.sortedKeys]), let line=String(data:data,encoding:.utf8) {
        print(line);fflush(stdout)
    }
}
let args=CommandLine.arguments
let selfTest=args.contains("--self-test")
guard CGPreflightScreenCaptureAccess() else {emit(["status":"restricted","reason":"screen_capture_permission_missing"]);exit(3)}
var testWindow:NSWindow?
var testLabel:NSTextField?
var windowID:CGWindowID=0
var roi=[0.22,0.12,0.77,0.64]
if selfTest {
    let app=NSApplication.shared
    app.setActivationPolicy(.regular)
    let w=NSWindow(contentRect:NSRect(x:120,y:120,width:640,height:420),styleMask:[.titled,.closable,.miniaturizable],backing:.buffered,defer:false)
    w.title="ChatLens Synthetic Watch Test"
    w.contentView?.wantsLayer=true;w.contentView?.layer?.backgroundColor=NSColor.white.cgColor
    let label=NSTextField(labelWithString:"SYNTHETIC 0 — NO REAL CHAT")
    label.font=NSFont.systemFont(ofSize:28);label.textColor = .black;label.frame=NSRect(x:80,y:180,width:500,height:70)
    w.contentView?.addSubview(label)
    w.makeKeyAndOrderFront(nil);app.activate(ignoringOtherApps:true)
    testWindow=w;testLabel=label;windowID=CGWindowID(w.windowNumber);roi=[0,0,1,1]
    if args.contains("--pause-test") {
        Timer.scheduledTimer(withTimeInterval:3,repeats:false){ _ in w.miniaturize(nil) }
        Timer.scheduledTimer(withTimeInterval:5,repeats:false){ _ in w.deminiaturize(nil);w.makeKeyAndOrderFront(nil);app.activate(ignoringOtherApps:true) }
    }
    var tick=0
    Timer.scheduledTimer(withTimeInterval:1.0,repeats:true){ _ in tick+=1;label.stringValue="SYNTHETIC \(tick) — NO REAL CHAT" }
    Timer.scheduledTimer(withTimeInterval:8,repeats:false){ _ in emit(["status":"self_test_finished"]);exit(0) }
} else {
    guard let marker=args.firstIndex(of:"--watch"),args.count>marker+1,let id=UInt32(args[marker+1]),id>0 else {
        emit(["status":"error","reason":"explicit_window_id_required"]);exit(2)
    }
    windowID=id
    if let marker=args.firstIndex(of:"--roi"),args.count>marker+1 {
        let parsed=args[marker+1].split(separator:",").compactMap{Double($0)}
        if parsed.count==4,parsed[0]>=0,parsed[1]>=0,parsed[2]>0,parsed[3]>0,parsed[0]+parsed[2]<=1,parsed[1]+parsed[3]<=1 {roi=parsed}
        else {emit(["status":"error","reason":"invalid_normalized_roi"]);exit(2)}
    }
}
let targetID=windowID
let watchROI=roi

class WatchOutput:NSObject,SCStreamOutput,SCStreamDelegate,@unchecked Sendable {
    let ownerPID:pid_t
    let windowID:CGWindowID
    let roi:[Double]
    var previous:SHA256.Digest?
    var frames=0
    var changes=0
    init(ownerPID:pid_t,windowID:CGWindowID,roi:[Double]){self.ownerPID=ownerPID;self.windowID=windowID;self.roi=roi}
    func available()->Bool {
        let session=CGSessionCopyCurrentDictionary() as? [String:Any] ?? [:]
        if session["CGSSessionScreenIsLocked"] as? Bool == true{return false}
        if NSWorkspace.shared.frontmostApplication?.processIdentifier != ownerPID {return false}
        let windows=CGWindowListCopyWindowInfo([.optionOnScreenOnly,.excludeDesktopElements],kCGNullWindowID) as? [[String:Any]] ?? []
        let owned=windows.filter{($0[kCGWindowOwnerPID as String] as? Int32)==ownerPID && ($0[kCGWindowLayer as String] as? Int)==0}
        return (owned.first?[kCGWindowNumber as String] as? UInt32)==windowID
    }
    func stream(_ stream:SCStream,didOutputSampleBuffer sampleBuffer:CMSampleBuffer,of outputType:SCStreamOutputType) {
        guard outputType == .screen,available(),CMSampleBufferIsValid(sampleBuffer),let pixel=CMSampleBufferGetImageBuffer(sampleBuffer) else{return}
        CVPixelBufferLockBaseAddress(pixel,.readOnly);defer{CVPixelBufferUnlockBaseAddress(pixel,.readOnly)}
        guard let base=CVPixelBufferGetBaseAddress(pixel) else{return}
        let width=CVPixelBufferGetWidth(pixel),height=CVPixelBufferGetHeight(pixel),stride=CVPixelBufferGetBytesPerRow(pixel)
        let left=Int(Double(width)*roi[0]),top=Int(Double(height)*roi[1]),right=min(width,Int(Double(width)*(roi[0]+roi[2]))),bottom=min(height,Int(Double(height)*(roi[1]+roi[3])))
        guard left<right,top<bottom else{return}
        var hasher=SHA256()
        for row in Swift.stride(from:top,to:bottom,by:2) {
            let ptr=base.advanced(by:row*stride+left*4)
            hasher.update(bufferPointer:UnsafeRawBufferPointer(start:ptr,count:(right-left)*4))
        }
        let digest=hasher.finalize();frames+=1
        if let old=previous {
            if old != digest {changes+=1;emit(["status":"changed","reason":"local_roi_pixels_changed","frames":frames,"changes":changes,"roi":roi,"image_saved":false,"model_called":false])}
        } else {emit(["status":"ready","frames":frames,"roi":roi,"image_saved":false,"model_called":false])}
        previous=digest
    }
    func stream(_ stream:SCStream,didStopWithError error:Error){emit(["status":"restricted","reason":"stream_stopped"])}
}
var retainedOutput:WatchOutput?
var retainedStream:SCStream?
var active=false
var busy=false
var previousAvailability:Bool?
Task { @MainActor in
    do {
        let content=try await SCShareableContent.excludingDesktopWindows(true,onScreenWindowsOnly:true)
        guard let target=content.windows.first(where:{$0.windowID==targetID}),let owner=target.owningApplication else {emit(["status":"restricted","reason":"selected_window_unavailable"]);exit(3)}
        let out=WatchOutput(ownerPID:owner.processID,windowID:targetID,roi:watchROI)
        let config=SCStreamConfiguration();config.width=512;config.height=max(1,Int(target.frame.height/target.frame.width*512));config.minimumFrameInterval=CMTime(value:1,timescale:1);config.pixelFormat=kCVPixelFormatType_32BGRA;config.queueDepth=3;config.showsCursor=false;config.capturesAudio=false
        let stream=SCStream(filter:SCContentFilter(desktopIndependentWindow:target),configuration:config,delegate:out)
        try stream.addStreamOutput(out,type:.screen,sampleHandlerQueue:DispatchQueue(label:"chatlens.local-watch"))
        retainedOutput=out;retainedStream=stream
        Timer.scheduledTimer(withTimeInterval:0.5,repeats:true){ _ in
            let available=out.available()
            if previousAvailability != available {emit(["status":available ? "eligible":"paused","reason":available ? "selected_window_foreground":"window_not_foreground_or_locked_or_hidden"]);previousAvailability=available}
            if busy || available==active{return}
            busy=true
            Task { @MainActor in
                do {if available {try await stream.startCapture()} else {try await stream.stopCapture()};active=available}
                catch {emit(["status":"restricted","reason":"stream_start_or_stop_failed"])}
                busy=false
            }
        }
    } catch {emit(["status":"restricted","reason":"screen_content_unavailable"]);exit(3)}
}
RunLoop.main.run()
