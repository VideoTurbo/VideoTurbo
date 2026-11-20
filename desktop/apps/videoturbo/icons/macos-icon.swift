// 生成 macOS 应用图标（icons/icon.icns）。图形与 docs/images/brand/avatar.svg 相同，按 macOS 图标规范加了
// 留白、连续圆角和阴影；Windows 仍用 icon.ico。改了图标后在 macOS 上运行：
//
//   swiftc -O -o /tmp/draw-icon desktop/apps/videoturbo/icons/macos-icon.swift
//   I=/tmp/VideoTurbo.iconset; mkdir -p $I
//   for spec in "16 16 small" "16@2x 32 small" "32 32 small" "32@2x 64 small" "128 128" "128@2x 256" \
//               "256 256" "256@2x 512" "512 512" "512@2x 1024"; do
//     set -- $spec; n=${1%@2x}; suffix=${1#$n}
//     /tmp/draw-icon "$I/icon_${n}x${n}${suffix}.png" $2 $3
//   done
//   iconutil -c icns -o desktop/apps/videoturbo/icons/icon.icns $I
//
// 参数：输出文件、边长（像素）、可选的 small（小尺寸去掉速度线、放大字标）。
import AppKit
import CoreGraphics

// macOS 应用图标：1024 画布，824 的连续圆角方块（超椭圆 n=5，接近系统图标的外形），四周留白放阴影。
let size = CGFloat(Int(CommandLine.arguments[2]) ?? 1024)
// 小尺寸（Finder 列表、菜单）去掉速度线、字标放大，免得糊成一团。
let small = CommandLine.arguments.count > 3 && CommandLine.arguments[3] == "small"
let k = size / 1024
let space = CGColorSpace(name: CGColorSpace.sRGB)!
let ctx = CGContext(data: nil, width: Int(size), height: Int(size), bitsPerComponent: 8, bytesPerRow: 0,
                    space: space, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
ctx.setShouldAntialias(true)
ctx.interpolationQuality = .high
// 以左上角为原点、y 向下，与 SVG 坐标一致。
ctx.translateBy(x: 0, y: size)
ctx.scaleBy(x: k, y: -k)

func color(_ hex: UInt32, _ alpha: CGFloat = 1) -> CGColor {
    CGColor(srgbRed: CGFloat((hex >> 16) & 0xff) / 255, green: CGFloat((hex >> 8) & 0xff) / 255,
            blue: CGFloat(hex & 0xff) / 255, alpha: alpha)
}

func squircle(_ rect: CGRect, n: CGFloat = 5) -> CGPath {
    let path = CGMutablePath()
    let a = rect.width / 2, b = rect.height / 2, cx = rect.midX, cy = rect.midY
    let steps = 1440
    for i in 0...steps {
        let t = CGFloat(i) / CGFloat(steps) * 2 * .pi
        let c = cos(t), s = sin(t)
        let x = cx + a * (c < 0 ? -1 : 1) * pow(abs(c), 2 / n)
        let y = cy + b * (s < 0 ? -1 : 1) * pow(abs(s), 2 / n)
        if i == 0 { path.move(to: CGPoint(x: x, y: y)) } else { path.addLine(to: CGPoint(x: x, y: y)) }
    }
    path.closeSubpath()
    return path
}

let tileRect = CGRect(x: 100, y: 100, width: 824, height: 824)
let tile = squircle(tileRect)

// 方块阴影：轻微向下，模拟系统图标的投影。setShadow 的偏移不受坐标变换影响，向下是负值。
ctx.saveGState()
ctx.setShadow(offset: CGSize(width: 0, height: -10 * k), blur: 24 * k, color: color(0x0b0f3a, 0.30))
ctx.addPath(tile)
ctx.setFillColor(color(0x4a52c8))
ctx.fillPath()
ctx.restoreGState()

// 渐变底色 + 左上角的柔光。
ctx.saveGState()
ctx.addPath(tile)
ctx.clip()
let bg = CGGradient(colorsSpace: space, colors: [color(0x3b44b3), color(0x5a63e0), color(0x959cff)] as CFArray,
                    locations: [0, 0.55, 1])!
ctx.drawLinearGradient(bg, start: CGPoint(x: 100, y: 100), end: CGPoint(x: 924, y: 924), options: [])
let glow = CGGradient(colorsSpace: space, colors: [color(0xffffff, 0.20), color(0xffffff, 0)] as CFArray,
                      locations: [0, 1])!
ctx.drawRadialGradient(glow, startCenter: CGPoint(x: 330, y: 230), startRadius: 0,
                       endCenter: CGPoint(x: 330, y: 230), endRadius: 640, options: [])
ctx.restoreGState()

// 边缘高光：上方亮、下方渐隐的细描边。
ctx.saveGState()
ctx.addPath(squircle(tileRect.insetBy(dx: 1.5, dy: 1.5)))
ctx.setLineWidth(3)
ctx.replacePathWithStrokedPath()
ctx.clip()
let rim = CGGradient(colorsSpace: space, colors: [color(0xffffff, 0.40), color(0xffffff, 0.05), color(0xffffff, 0)] as CFArray,
                     locations: [0, 0.45, 1])!
ctx.drawLinearGradient(rim, start: CGPoint(x: 0, y: 100), end: CGPoint(x: 0, y: 924), options: [])
ctx.restoreGState()

// VT 字标（斜体）与速度线：坐标与 docs/images/brand/avatar.svg 相同。
ctx.saveGState()
ctx.translateBy(x: 512, y: 512)
if small { ctx.scaleBy(x: 1.2, y: 1.2) }
ctx.concatenate(CGAffineTransform(a: 1, b: 0, c: tan(-12 * .pi / 180), d: 1, tx: 0, ty: 0))
ctx.translateBy(x: small ? -210 : -168, y: -100)
ctx.setLineCap(.round)
ctx.setLineWidth(22)
for (x1, y, x2, alpha) in small ? [] : [(-84.0, 36.0, -26.0, 0.5), (-80, 100, 2, 0.75), (-14, 164, 30, 0.5)] {
    ctx.setStrokeColor(color(0xffffff, CGFloat(alpha)))
    ctx.move(to: CGPoint(x: x1, y: y))
    ctx.addLine(to: CGPoint(x: x2, y: y))
    ctx.strokePath()
}
let glyph = CGMutablePath()
glyph.addLines(between: [CGPoint(x: 0, y: 0), CGPoint(x: 64, y: 0), CGPoint(x: 110, y: 104), CGPoint(x: 156, y: 0),
                         CGPoint(x: 220, y: 0), CGPoint(x: 131, y: 200), CGPoint(x: 89, y: 200)])
glyph.closeSubpath()
glyph.addLines(between: [CGPoint(x: 238, y: 0), CGPoint(x: 420, y: 0), CGPoint(x: 420, y: 58), CGPoint(x: 358, y: 58),
                         CGPoint(x: 358, y: 200), CGPoint(x: 298, y: 200), CGPoint(x: 298, y: 58), CGPoint(x: 238, y: 58)])
glyph.closeSubpath()
ctx.setShadow(offset: CGSize(width: 0, height: -8 * k), blur: 18 * k, color: color(0x1e2470, 0.35))
ctx.addPath(glyph)
ctx.setFillColor(color(0xffffff))
ctx.fillPath()
ctx.restoreGState()

let image = ctx.makeImage()!
let rep = NSBitmapImageRep(cgImage: image)
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: CommandLine.arguments[1]))
print("ok")
