import Foundation
import AppKit
import ApplicationServices
import Darwin

let args = CommandLine.arguments
let name = args.contains("qq") ? "qq" : "wechat"
let bundle = name == "qq" ? "com.tencent.qq" : "com.tencent.xinWeChat"
let capture = args.contains("--capture")
let apps = NSRunningApplication.runningApplications(withBundleIdentifier: bundle)
let trusted = AXIsProcessTrusted()
func attr(_ e: AXUIElement, _ key: String) -> AnyObject? {
    var value: CFTypeRef?
    return AXUIElementCopyAttributeValue(e, key as CFString, &value) == .success ? value : nil
}
var result: [String: Any] = ["platform":name,"os":"macOS","accessibility_trusted": trusted,"running": !apps.isEmpty,"capture_mode":"accessibility_visible","scope":"selected visible window only; no history or background guarantee","screen_capture_permission": CGPreflightScreenCaptureAccess()]
result["matching_applications"] = apps.count
if let app = apps.first {
    result["process_id"] = Int(app.processIdentifier)
    // NSRunningApplication.launchDate can be nil for a directly restored app.
    // Kernel process metadata provides the same identity after either launch.
    var processInfo = proc_bsdinfo()
    let infoSize = MemoryLayout<proc_bsdinfo>.stride
    if proc_pidinfo(app.processIdentifier, PROC_PIDTBSDINFO, 0, &processInfo, Int32(infoSize)) == infoSize {
        result["process_started_at"] = Double(processInfo.pbi_start_tvsec) + Double(processInfo.pbi_start_tvusec) / 1_000_000
    }
    result["version"] = app.bundleURL.flatMap { Bundle(url: $0)?.infoDictionary?["CFBundleShortVersionString"] as? String } ?? "unknown"
    result["bundle_id"] = bundle
    let session = CGSessionCopyCurrentDictionary() as? [String:Any] ?? [:]
    result["screen_state_available"] = !session.isEmpty
    result["screen_locked"] = session["CGSSessionScreenIsLocked"] as? Bool ?? false
    let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly,.excludeDesktopElements], kCGNullWindowID) as? [[String:Any]] ?? []
    result["onscreen_windows"] = list.filter { ($0[kCGWindowOwnerPID as String] as? Int32) == app.processIdentifier && ($0[kCGWindowLayer as String] as? Int) == 0 }.map { info -> [String:Any] in
        return ["window_id":info[kCGWindowNumber as String] as? Int ?? 0,"bounds":info[kCGWindowBounds as String] ?? [:],"alpha":info[kCGWindowAlpha as String] as? Double ?? 0]
    }
    result["frontmost"] = app.isActive
    result["hidden"] = app.isHidden
    let e = AXUIElementCreateApplication(app.processIdentifier)
    var windowsObj: CFTypeRef?
    let status = AXUIElementCopyAttributeValue(e, kAXWindowsAttribute as CFString, &windowsObj)
    result["ax_error"] = status.rawValue
    let windows = windowsObj as? [AXUIElement] ?? []
    result["window_count"] = windows.count
    var summaries: [[String:Any]] = []
    for (i, w) in windows.enumerated() {
        var roles: [String:Int] = [:]
        var strings: [String] = []
        var nodeCount = 0
        func visit(_ n: AXUIElement, _ depth: Int) {
            if depth > 15 || nodeCount >= 2000 { return }
            nodeCount += 1
            let role = attr(n,kAXRoleAttribute) as? String ?? "unknown"
            roles[role, default:0] += 1
            // Text is never emitted by the default metadata probe.
            if capture && (role == "AXStaticText" || role == "AXTextArea") {
                if let s = attr(n,kAXValueAttribute) as? String, !s.isEmpty { strings.append(s) }
                else if let s = attr(n,kAXTitleAttribute) as? String, !s.isEmpty { strings.append(s) }
            }
            for child in attr(n,kAXChildrenAttribute) as? [AXUIElement] ?? [] { visit(child,depth+1) }
        }
        visit(w,0)
        var summary: [String:Any] = ["index":i,"minimized":attr(w,kAXMinimizedAttribute) as? Bool ?? false,"nodes":nodeCount,"roles":roles,"text_nodes":strings.count,"text_characters":strings.reduce(0){$0+$1.count}]
        if capture { summary["fragments"] = strings }
        summaries.append(summary)
    }
    result["windows"] = summaries
}
let data = try JSONSerialization.data(withJSONObject:result, options:[.sortedKeys])
print(String(data:data,encoding:.utf8)!)
