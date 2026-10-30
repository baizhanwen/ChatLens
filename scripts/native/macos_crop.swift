import Foundation
import AppKit
func crop(_ data:Data,_ roi:[Double]) throws -> Data {
    guard let rep=NSBitmapImageRep(data:data),let image=rep.cgImage,roi.count==4,roi[0]>=0,roi[1]>=0,roi[2]>0,roi[3]>0,roi[0]+roi[2]<=1,roi[1]+roi[3]<=1 else{throw NSError(domain:"invalid_crop",code:1)}
    let rect=CGRect(x:floor(Double(image.width)*roi[0]),y:floor(Double(image.height)*roi[1]),width:floor(Double(image.width)*roi[2]),height:floor(Double(image.height)*roi[3]))
    guard let clipped=image.cropping(to:rect),let out=NSBitmapImageRep(cgImage:clipped).representation(using:.png,properties:[:]) else{throw NSError(domain:"crop_failed",code:2)}
    return out
}
let args=CommandLine.arguments
do {
    if args.contains("--self-test") {
        let rep=NSBitmapImageRep(bitmapDataPlanes:nil,pixelsWide:100,pixelsHigh:80,bitsPerSample:8,samplesPerPixel:4,hasAlpha:true,isPlanar:false,colorSpaceName:.deviceRGB,bytesPerRow:400,bitsPerPixel:32)!
        for y in 0..<80 {for x in 0..<100 {let i=y*rep.bytesPerRow+x*4;rep.bitmapData![i]=x<50 ? 255:0;rep.bitmapData![i+1]=0;rep.bitmapData![i+2]=x<50 ? 0:255;rep.bitmapData![i+3]=255}}
        let data=rep.representation(using:.png,properties:[:])!
        let cropped=try crop(data,[0.5,0,0.5,1]);let check=NSBitmapImageRep(data:cropped)!
        let good=check.pixelsWide==50 && check.pixelsHigh==80 && check.bitmapData![0]<5 && check.bitmapData![2]>250
        print("{\"status\":\"\(good ? "passed":"failed")\",\"synthetic_only\":true,\"source_width\":100,\"source_height\":80,\"cropped_width\":\(check.pixelsWide),\"cropped_height\":\(check.pixelsHigh),\"left_private_region_removed\":\(good)}")
        exit(good ? 0:1)
    }
    guard let i=args.firstIndex(of:"--roi"),args.count>i+1 else{throw NSError(domain:"crop_required",code:3)}
    let roi=args[i+1].split(separator:",").compactMap{Double($0)}
    let data=FileHandle.standardInput.readDataToEndOfFile()
    guard data.count<=12_000_000 else{throw NSError(domain:"image_too_large",code:4)}
    FileHandle.standardOutput.write(try crop(data,roi))
} catch {fputs("image_crop_failed\n",stderr);exit(2)}
