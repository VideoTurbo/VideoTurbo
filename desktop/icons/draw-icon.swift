// 画桌面版图标（主程序和 VideoTurbo Worker）。由 desktop/icons/make-icons.sh 调用，不要直接运行。
//
// 参数：输出文件、边长（像素）、可选的 small（32 像素及以下去掉速度线、放大字标）。
// 环境变量：VT_BADGE=1 加显卡芯片角标（VideoTurbo Worker）；VT_MARGIN 圆角方块四周的留白
// （1024 画布里，macOS 规范是 100，Windows 用 56）；VT_THEME=teal 换青绿配色（备用）。
import AppKit
import CoreGraphics

// macOS 应用图标：1024 画布，824 的连续圆角方块（超椭圆 n=5，接近系统图标的外形），四周留白放阴影。
let size = CGFloat(Int(CommandLine.arguments[2]) ?? 1024)
// 小尺寸（Finder 列表、菜单）去掉速度线、字标放大，免得糊成一团。
let small = CommandLine.arguments.count > 3 && CommandLine.arguments[3] == "small"
let envv = ProcessInfo.processInfo.environment
let theme = envv["VT_THEME"] ?? "purple"
let badge = envv["VT_BADGE"] == "1"
let margin = CGFloat(Double(envv["VT_MARGIN"] ?? "100") ?? 100)
let palette: [UInt32] = theme == "teal" ? [0x0b5d57, 0x0f9488, 0x5fd8c7] : [0x3b44b3, 0x5a63e0, 0x959cff]
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

let tileRect = CGRect(x: margin, y: margin, width: 1024 - 2 * margin, height: 1024 - 2 * margin)
let tile = squircle(tileRect)

// 方块阴影：轻微向下，模拟系统图标的投影。setShadow 的偏移不受坐标变换影响，向下是负值。
ctx.saveGState()
ctx.setShadow(offset: CGSize(width: 0, height: -10 * k), blur: 24 * k, color: color(0x0b0f3a, 0.30))
ctx.addPath(tile)
ctx.setFillColor(color(palette[1]))
ctx.fillPath()
ctx.restoreGState()

// 渐变底色 + 左上角的柔光。
ctx.saveGState()
ctx.addPath(tile)
ctx.clip()
let bg = CGGradient(colorsSpace: space, colors: palette.map { color($0) } as CFArray,
                    locations: [0, 0.55, 1])!
ctx.drawLinearGradient(bg, start: CGPoint(x: tileRect.minX, y: tileRect.minY), end: CGPoint(x: tileRect.maxX, y: tileRect.maxY), options: [])
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
ctx.drawLinearGradient(rim, start: CGPoint(x: 0, y: tileRect.minY), end: CGPoint(x: 0, y: tileRect.maxY), options: [])
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

if badge {
    let r: CGFloat = small ? 230 : 190
    let c = CGPoint(x: tileRect.maxX - r * 0.78, y: tileRect.maxY - r * 0.78)
    ctx.saveGState()
    ctx.setShadow(offset: CGSize(width: 0, height: -6 * k), blur: 14 * k, color: color(0x0b0f3a, 0.35))
    ctx.setFillColor(color(0xffffff))
    ctx.fillEllipse(in: CGRect(x: c.x - r, y: c.y - r, width: 2 * r, height: 2 * r))
    ctx.restoreGState()
    ctx.setFillColor(color(0x16a34a))
    let inner = r * 0.84
    ctx.fillEllipse(in: CGRect(x: c.x - inner, y: c.y - inner, width: 2 * inner, height: 2 * inner))
    // 显卡芯片：圆角方块 + 四边引脚
    let chip = r * 0.82
    let body = CGRect(x: c.x - chip / 2, y: c.y - chip / 2, width: chip, height: chip)
    ctx.setStrokeColor(color(0xffffff))
    ctx.setLineWidth(r * 0.1)
    ctx.addPath(CGPath(roundedRect: body, cornerWidth: chip * 0.18, cornerHeight: chip * 0.18, transform: nil))
    ctx.strokePath()
    ctx.setLineCap(.round)
    for i in 0..<3 {
        let off = (CGFloat(i) - 1) * chip * 0.28
        for (a, b) in [((c.x + off, body.minY), (c.x + off, body.minY - r * 0.16)), ((c.x + off, body.maxY), (c.x + off, body.maxY + r * 0.16)),
                       ((body.minX, c.y + off), (body.minX - r * 0.16, c.y + off)), ((body.maxX, c.y + off), (body.maxX + r * 0.16, c.y + off))] {
            ctx.move(to: CGPoint(x: a.0, y: a.1)); ctx.addLine(to: CGPoint(x: b.0, y: b.1))
        }
    }
    ctx.strokePath()
    let core = chip * 0.34
    ctx.setFillColor(color(0xffffff))
    ctx.fill(CGRect(x: c.x - core / 2, y: c.y - core / 2, width: core, height: core))
}

let image = ctx.makeImage()!
let rep = NSBitmapImageRep(cgImage: image)
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: CommandLine.arguments[1]))
print("ok")
