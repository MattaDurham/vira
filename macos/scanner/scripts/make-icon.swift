import AppKit
import Foundation

let directory = URL(fileURLWithPath: CommandLine.arguments[1])
try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
for (name, size) in [("icon_16x16", 16), ("icon_16x16@2x", 32), ("icon_32x32", 32), ("icon_32x32@2x", 64), ("icon_128x128", 128), ("icon_128x128@2x", 256), ("icon_256x256", 256), ("icon_256x256@2x", 512), ("icon_512x512", 512), ("icon_512x512@2x", 1024)] {
    let image = NSImage(size: NSSize(width: size, height: size))
    image.lockFocus()
    let s = Double(size) / 1024
    let transform = NSAffineTransform(); transform.scale(by: s); transform.concat()
    NSColor(calibratedRed: 0.18, green: 0.34, blue: 0.27, alpha: 1).setFill()
    NSBezierPath(roundedRect: NSRect(x: 60, y: 60, width: 904, height: 904), xRadius: 200, yRadius: 200).fill()
    NSColor(calibratedWhite: 0.98, alpha: 1).setFill()
    NSBezierPath(roundedRect: NSRect(x: 310, y: 230, width: 404, height: 564), xRadius: 30, yRadius: 30).fill()
    NSColor(calibratedRed: 0.18, green: 0.34, blue: 0.27, alpha: 0.45).setStroke()
    for y in [630, 550, 470] { let path = NSBezierPath(); path.lineWidth = 17; path.lineCapStyle = .round; path.move(to: NSPoint(x: 390, y: y)); path.line(to: NSPoint(x: 634, y: y)); path.stroke() }
    NSColor.white.withAlphaComponent(0.8).setStroke()
    for (x, y, dx, dy) in [(235.0, 860.0, 90.0, -90.0), (789, 860, -90, -90), (235, 164, 90, 90), (789, 164, -90, 90)] {
        let path = NSBezierPath(); path.lineWidth = 20; path.lineCapStyle = .round; path.lineJoinStyle = .round
        path.move(to: NSPoint(x: x + dx, y: y)); path.line(to: NSPoint(x: x, y: y)); path.line(to: NSPoint(x: x, y: y + dy)); path.stroke()
    }
    image.unlockFocus()
    let rep = NSBitmapImageRep(data: image.tiffRepresentation!)!
    try rep.representation(using: .png, properties: [:])!.write(to: directory.appendingPathComponent(name + ".png"))
}
