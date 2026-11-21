// 画桌面版的图标和安装程序图片。由 desktop/icons/make-icons.sh 调用，不要直接运行。
//
// 图形：靛蓝圆角方块上，五根声波条排成 V 字，下面一条字幕（语音识别成字幕）。坐标以 120×120 的格子定义，
// 与 desktop/icons/logo.svg 相同。
//
// 用法：
//   draw-icon 输出.png 边长 [small]     应用图标；small（32 像素及以下）改用三根粗条，免得糊成一团
//   draw-icon 输出.png 边长 tray         macOS 菜单栏的单色模板图标（黑色、透明底）
//   draw-icon 输出.png 边长 avatar       头像（GitHub 等）：铺满的方形，图形留在圆形裁切范围内
//   draw-icon 输出.bmp header|sidebar [名称]  Windows 安装程序的顶部（150×57）和侧边（164×314）图片，24 位 BMP
// 环境变量：VT_BADGE=1 右下角加显卡芯片角标（VideoTurbo Worker）；VT_MARGIN 圆角方块四周的留白
// （1024 画布里，macOS 规范是 100，Windows 用 56）。
import AppKit
import CoreGraphics

let args = CommandLine.arguments
let envv = ProcessInfo.processInfo.environment
let badge = envv["VT_BADGE"] == "1"
let margin = CGFloat(Double(envv["VT_MARGIN"] ?? "100") ?? 100)
let mode = args.count > 3 ? args[3] : ""
let installer = args[2] == "header" || args[2] == "sidebar"
let canvas: CGSize = args[2] == "header" ? CGSize(width: 150, height: 57)
    : args[2] == "sidebar" ? CGSize(width: 164, height: 314)
    : CGSize(width: Int(args[2]) ?? 1024, height: Int(args[2]) ?? 1024)
let space = CGColorSpace(name: CGColorSpace.sRGB)!
let ctx = CGContext(data: nil, width: Int(canvas.width), height: Int(canvas.height), bitsPerComponent: 8,
                    bytesPerRow: 0, space: space, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
ctx.setShouldAntialias(true)
ctx.interpolationQuality = .high
// 以左上角为原点、y 向下，与 SVG 坐标一致。
ctx.translateBy(x: 0, y: canvas.height)
ctx.scaleBy(x: 1, y: -1)

let brand: UInt32 = 0x3d47c4
func color(_ hex: UInt32, _ alpha: CGFloat = 1) -> CGColor {
    CGColor(srgbRed: CGFloat((hex >> 16) & 0xff) / 255, green: CGFloat((hex >> 8) & 0xff) / 255,
            blue: CGFloat(hex & 0xff) / 255, alpha: alpha)
}

/// 声波 V + 字幕条，画在 120×120 格子映射到的 rect 里。small：三根粗条。
func drawGlyph(in rect: CGRect, ink: UInt32, small: Bool) {
    let s = rect.width / 120
    func bar(_ x: CGFloat, _ y: CGFloat, _ w: CGFloat, _ h: CGFloat, _ alpha: CGFloat) {
        let r = CGRect(x: rect.minX + x * s, y: rect.minY + y * s, width: w * s, height: h * s)
        ctx.addPath(CGPath(roundedRect: r, cornerWidth: min(w, h) * s / 2, cornerHeight: min(w, h) * s / 2, transform: nil))
        ctx.setFillColor(color(ink, alpha))
        ctx.fillPath()
    }
    if small {
        for (x, y, h) in [(24.0, 22.0, 46.0), (52.0, 38.0, 36.0), (80.0, 22.0, 46.0)] { bar(x, y, 16, h, 1) }
        bar(24, 84, 72, 13, 0.75)
    } else {
        for (x, y, h) in [(22.0, 23.0, 44.0), (38.0, 33.0, 40.0), (55.0, 45.0, 34.0), (72.0, 33.0, 40.0), (88.0, 23.0, 44.0)] {
            bar(x, y, 10, h, 1)
        }
        bar(26, 89, 68, 8, 0.7)
    }
}

func roundedTile(_ rect: CGRect) -> CGPath {
    let radius = rect.width * 28 / 120
    return CGPath(roundedRect: rect, cornerWidth: radius, cornerHeight: radius, transform: nil)
}

/// 品牌底色：上浅下深的细微渐变。
func fillBrand(_ path: CGPath, _ rect: CGRect) {
    ctx.saveGState()
    ctx.addPath(path)
    ctx.clip()
    let gradient = CGGradient(colorsSpace: space, colors: [color(0x4852d4), color(brand), color(0x343db0)] as CFArray,
                              locations: [0, 0.5, 1])!
    ctx.drawLinearGradient(gradient, start: CGPoint(x: rect.midX, y: rect.minY), end: CGPoint(x: rect.midX, y: rect.maxY), options: [])
    ctx.restoreGState()
}

/// 显卡芯片角标（VideoTurbo Worker）：白边绿底圆，里面一枚带引脚的芯片。
func drawBadge(tile: CGRect, small: Bool) {
    let r = tile.width * (small ? 0.27 : 0.22)
    let c = CGPoint(x: tile.maxX - r * 0.78, y: tile.maxY - r * 0.78)
    ctx.setFillColor(color(0xffffff))
    ctx.fillEllipse(in: CGRect(x: c.x - r, y: c.y - r, width: 2 * r, height: 2 * r))
    ctx.setFillColor(color(0x16a34a))
    let inner = r * 0.84
    ctx.fillEllipse(in: CGRect(x: c.x - inner, y: c.y - inner, width: 2 * inner, height: 2 * inner))
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

func drawText(_ text: String, centerX: CGFloat, top: CGFloat, size: CGFloat, ink: UInt32) {
    // 文字用 AppKit 画，在 y 向上的原始坐标里画。
    let attrs: [NSAttributedString.Key: Any] = [
        .font: NSFont.systemFont(ofSize: size, weight: .semibold),
        .foregroundColor: NSColor(cgColor: color(ink))!,
    ]
    let string = NSAttributedString(string: text, attributes: attrs)
    let bounds = string.size()
    ctx.saveGState()
    ctx.translateBy(x: 0, y: canvas.height)
    ctx.scaleBy(x: 1, y: -1)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(cgContext: ctx, flipped: false)
    string.draw(at: CGPoint(x: centerX - bounds.width / 2, y: canvas.height - top - bounds.height))
    NSGraphicsContext.restoreGraphicsState()
    ctx.restoreGState()
}

if args[2] == "header" {
    // 安装向导顶部右侧：白底，右边一枚小图标。
    ctx.setFillColor(color(0xffffff))
    ctx.fill(CGRect(origin: .zero, size: canvas))
    let tile = CGRect(x: canvas.width - 52, y: 6.5, width: 44, height: 44)
    fillBrand(roundedTile(tile), tile)
    drawGlyph(in: tile, ink: 0xffffff, small: false)
    if badge { drawBadge(tile: tile, small: true) }
} else if args[2] == "sidebar" {
    // 欢迎页和完成页左侧：品牌色底，大图标 + 名称。
    let full = CGRect(origin: .zero, size: canvas)
    fillBrand(CGPath(rect: full, transform: nil), full)
    let tile = CGRect(x: 32, y: 74, width: 100, height: 100)
    ctx.addPath(roundedTile(tile))
    ctx.setFillColor(color(0xffffff, 0.14))
    ctx.fillPath()
    drawGlyph(in: tile, ink: 0xffffff, small: false)
    if badge { drawBadge(tile: tile, small: false) }
    drawText(mode.isEmpty ? "VideoTurbo" : mode, centerX: canvas.width / 2, top: 192, size: 17, ink: 0xffffff)
} else if mode == "avatar" {
    let full = CGRect(origin: .zero, size: canvas)
    fillBrand(CGPath(rect: full, transform: nil), full)
    drawGlyph(in: full.insetBy(dx: canvas.width * 0.07, dy: canvas.width * 0.07), ink: 0xffffff, small: false)
} else if mode == "tray" {
    // 菜单栏模板图标：只看透明度，系统按深浅色自动着色。
    let inset = canvas.width * 0.04
    drawGlyph(in: CGRect(x: inset, y: inset, width: canvas.width - 2 * inset, height: canvas.width - 2 * inset),
              ink: 0x000000, small: canvas.width <= 32)
} else {
    let k = canvas.width / 1024
    let tile = CGRect(x: margin * k, y: margin * k, width: (1024 - 2 * margin) * k, height: (1024 - 2 * margin) * k)
    let path = roundedTile(tile)
    if margin >= 80 {
        // macOS：方块下方的轻微投影，模拟系统图标。setShadow 的偏移不受坐标变换影响，向下是负值。
        ctx.saveGState()
        ctx.setShadow(offset: CGSize(width: 0, height: -10 * k), blur: 24 * k, color: color(0x0b0f3a, 0.30))
        ctx.addPath(path)
        ctx.setFillColor(color(brand))
        ctx.fillPath()
        ctx.restoreGState()
    }
    fillBrand(path, tile)
    drawGlyph(in: tile, ink: 0xffffff, small: mode == "small")
    if badge { drawBadge(tile: tile, small: mode == "small") }
}

let image = ctx.makeImage()!
let output = URL(fileURLWithPath: args[1])
if installer {
    // NSIS 要 24 位 BMP（不带透明通道），行从下往上存。
    let width = image.width, height = image.height
    let rep = NSBitmapImageRep(cgImage: image)
    let row = (width * 3 + 3) & ~3
    var data = Data()
    func le32(_ v: Int) { var x = UInt32(v).littleEndian; data.append(Data(bytes: &x, count: 4)) }
    func le16(_ v: Int) { var x = UInt16(v).littleEndian; data.append(Data(bytes: &x, count: 2)) }
    data.append(contentsOf: [0x42, 0x4d]); le32(54 + row * height); le32(0); le32(54)
    le32(40); le32(width); le32(height); le16(1); le16(24); le32(0); le32(row * height); le32(2835); le32(2835); le32(0); le32(0)
    for y in (0..<height).reversed() {
        var line = [UInt8]()
        for x in 0..<width {
            let c = rep.colorAt(x: x, y: y)!.usingColorSpace(.sRGB)!
            line += [UInt8(c.blueComponent * 255), UInt8(c.greenComponent * 255), UInt8(c.redComponent * 255)]
        }
        line += [UInt8](repeating: 0, count: row - width * 3)
        data.append(contentsOf: line)
    }
    try! data.write(to: output)
} else {
    let rep = NSBitmapImageRep(cgImage: image)
    try! rep.representation(using: .png, properties: [:])!.write(to: output)
}
print("ok")
