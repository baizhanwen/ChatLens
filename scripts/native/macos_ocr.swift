import Foundation
import Vision
import AppKit
let args = CommandLine.arguments
if args.count < 2 { fputs("image path required\n",stderr); exit(2) }
let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.recognitionLanguages = ["zh-Hans","en-US"]
request.usesLanguageCorrection = false
let handler = VNImageRequestHandler(url: URL(fileURLWithPath: args[1]), options: [:])
do {
    try handler.perform([request])
    let observations = request.results ?? []
    var out: [String:Any] = ["count": observations.count,"mean_confidence": observations.isEmpty ? 0 : observations.reduce(0.0){$0 + Double($1.topCandidates(1).first?.confidence ?? 0)} / Double(observations.count),"scope":"one explicitly selected window; OCR fragments; sender/time not established"]
    if args.contains("--capture") {
        out["fragments"] = observations.compactMap { o -> [String:Any]? in
            guard let t = o.topCandidates(1).first else { return nil }
            return ["text":t.string,"confidence":t.confidence,"box":[o.boundingBox.origin.x,o.boundingBox.origin.y,o.boundingBox.width,o.boundingBox.height]]
        }
    }
    print(String(data:try JSONSerialization.data(withJSONObject:out,options:[.sortedKeys]),encoding:.utf8)!)
} catch { print("{\"error\":\"ocr_failed\"}"); exit(1) }
