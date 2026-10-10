// Throwaway WKWebView probe for the ProfilePilot WebKit spike. Not product code.
// Usage: probe persist1 | persist2 | main <lan-ip>
import AppKit
import Network
import WebKit

let HTTP = "http://127.0.0.1:47801"
let ID_A = UUID(uuidString: "A0000000-0000-4000-8000-00000000000A")!
let ID_B = UUID(uuidString: "B0000000-0000-4000-8000-00000000000B")!
let ID_C = UUID(uuidString: "C0000000-0000-4000-8000-00000000000C")!
let ID_P = UUID(uuidString: "D0000000-0000-4000-8000-00000000000D")!
let ID_Q = UUID(uuidString: "E0000000-0000-4000-8000-00000000000E")!

func out(_ s: String) { print(s); fflush(stdout) }
func pause(_ s: Double) async { try? await Task.sleep(nanoseconds: UInt64(s * 1_000_000_000)) }

func safariVersion() -> String {
    let d = NSDictionary(contentsOfFile: "/Applications/Safari.app/Contents/Info.plist")
    return (d?["CFBundleShortVersionString"] as? String) ?? "26.0"
}

final class Dialogs: NSObject, WKUIDelegate {
    var log: [String] = []
    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        log.append("alert:\(message)"); completionHandler()
    }
    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        log.append("confirm:\(message)"); completionHandler(true)
    }
    func webView(_ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String, defaultText: String?,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (String?) -> Void) {
        log.append("prompt:\(prompt)"); completionHandler("probe-answer")
    }
    @objc(_webView:getWindowFrameWithCompletionHandler:)
    func ppWindowFrame(_ webView: WKWebView, completionHandler: @escaping (CGRect) -> Void) {
        completionHandler(webView.window?.frame ?? .zero)
    }
}

@MainActor final class FrameCollector: NSObject, WKScriptMessageHandler {
    var frames: [WKFrameInfo] = []
    func userContentController(_ ucc: WKUserContentController, didReceive message: WKScriptMessage) {
        frames.append(message.frameInfo)
    }
}

let dialogs = Dialogs()
@MainActor let ppWorld = WKContentWorld.world(name: "profilepilot")

@MainActor func makeView(_ store: WKWebsiteDataStore, collector: FrameCollector? = nil,
              rect: NSRect = NSRect(x: 0, y: 0, width: 1000, height: 700)) -> (NSWindow, WKWebView) {
    let cfg = WKWebViewConfiguration()
    cfg.websiteDataStore = store
    cfg.applicationNameForUserAgent = "Version/\(safariVersion()) Safari/605.1.15"
    if let collector {
        let src = "window.webkit.messageHandlers.pp.postMessage(location.href)"
        cfg.userContentController.addUserScript(WKUserScript(source: src, injectionTime: .atDocumentEnd,
                                                             forMainFrameOnly: false, in: ppWorld))
        cfg.userContentController.add(collector, contentWorld: ppWorld, name: "pp")
    }
    let wv = WKWebView(frame: rect, configuration: cfg)
    wv.uiDelegate = dialogs
    let win = NSWindow(contentRect: rect, styleMask: [.titled, .closable, .resizable, .miniaturizable],
                       backing: .buffered, defer: false)
    win.isReleasedWhenClosed = false
    win.contentView = wv
    return (win, wv)
}

@MainActor func load(_ wv: WKWebView, _ url: String, settle: Double = 1.5) async {
    wv.load(URLRequest(url: URL(string: url)!))
    await pause(0.3)
    let deadline = Date().addingTimeInterval(10)
    while wv.isLoading && Date() < deadline { await pause(0.1) }
    await pause(settle)
}

@MainActor func js(_ wv: WKWebView, _ body: String, world: WKContentWorld? = nil, frame: WKFrameInfo? = nil) async -> String {
    do {
        let r = try await wv.callAsyncJavaScript(body, arguments: [:], in: frame, contentWorld: world ?? .page)
        if let r, JSONSerialization.isValidJSONObject(r),
           let d = try? JSONSerialization.data(withJSONObject: r), let s = String(data: d, encoding: .utf8) { return s }
        return String(describing: r ?? "nil")
    } catch { return "ERR: \(error.localizedDescription)" }
}

@MainActor func cookieNames(_ store: WKWebsiteDataStore) async -> String {
    let cookies = await store.httpCookieStore.allCookies()
    return cookies.map { "\($0.name)=\($0.value)\($0.isHTTPOnly ? "(HttpOnly)" : "")@\($0.domain)" }.sorted().joined(separator: ", ")
}

// MARK: phases

@MainActor func persist1() async {
    let (_, va) = makeView(WKWebsiteDataStore(forIdentifier: ID_A))
    let (_, vb) = makeView(WKWebsiteDataStore(forIdentifier: ID_B))
    await load(va, "\(HTTP)/set?v=A")
    await load(vb, "\(HTTP)/set?v=B")
    out("persist1 A cookies: \(await cookieNames(WKWebsiteDataStore(forIdentifier: ID_A)))")
    out("persist1 B cookies: \(await cookieNames(WKWebsiteDataStore(forIdentifier: ID_B)))")
    await pause(3)  // let the network process flush to disk
}

@MainActor func persist2() async {
    for (tag, id) in [("A", ID_A), ("B", ID_B), ("C", ID_C)] {
        let store = WKWebsiteDataStore(forIdentifier: id)
        out("persist2 \(tag) cookie store: [\(await cookieNames(store))]")
        let (_, v) = makeView(store)
        await load(v, "\(HTTP)/get?tag=\(tag)")
    }
    // cookie API write + delete
    let a = WKWebsiteDataStore(forIdentifier: ID_A)
    let c = HTTPCookie(properties: [.name: "api_set", .value: "1", .domain: "127.0.0.1", .path: "/"])!
    await a.httpCookieStore.setCookie(c)
    out("persist2 A after setCookie: [\(await cookieNames(a))]")
    await a.httpCookieStore.deleteCookie(c)
    out("persist2 A after deleteCookie: [\(await cookieNames(a))]")
    let ids = await WKWebsiteDataStore.allDataStoreIdentifiers
    out("persist2 data store identifiers: \(ids.map { $0.uuidString }.sorted())")
}

@MainActor func sendClick(_ win: NSWindow, _ wv: WKWebView, x: Double, y: Double, direct: Bool) {
    let p = NSPoint(x: x, y: Double(wv.bounds.height) - y)
    let t = ProcessInfo.processInfo.systemUptime
    let down = NSEvent.mouseEvent(with: .leftMouseDown, location: p, modifierFlags: [], timestamp: t,
                                  windowNumber: win.windowNumber, context: nil, eventNumber: 1, clickCount: 1, pressure: 1)!
    let up = NSEvent.mouseEvent(with: .leftMouseUp, location: p, modifierFlags: [], timestamp: t + 0.05,
                                windowNumber: win.windowNumber, context: nil, eventNumber: 2, clickCount: 1, pressure: 0)!
    if direct { wv.mouseDown(with: down); wv.mouseUp(with: up) } else { win.sendEvent(down); win.sendEvent(up) }
}

@MainActor func sendKey(_ win: NSWindow, _ wv: WKWebView, _ ch: String, code: UInt16, direct: Bool) {
    let t = ProcessInfo.processInfo.systemUptime
    let down = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: [], timestamp: t, windowNumber: win.windowNumber,
                                context: nil, characters: ch, charactersIgnoringModifiers: ch, isARepeat: false, keyCode: code)!
    let up = NSEvent.keyEvent(with: .keyUp, location: .zero, modifierFlags: [], timestamp: t + 0.03, windowNumber: win.windowNumber,
                              context: nil, characters: ch, charactersIgnoringModifiers: ch, isARepeat: false, keyCode: code)!
    if direct { wv.keyDown(with: down); wv.keyUp(with: up) } else { win.sendEvent(down); win.sendEvent(up) }
}

@MainActor func inputTest(placement: String, direct: Bool) async {
    let (win, wv) = makeView(WKWebsiteDataStore(forIdentifier: ID_A))
    switch placement {
    case "behind": win.setFrameOrigin(NSPoint(x: 80, y: 80)); win.orderBack(nil)
    case "offscreen": win.setFrameOrigin(NSPoint(x: -32000, y: -32000)); win.orderBack(nil)
    default: break  // "nowindow": never ordered in
    }
    await load(wv, "\(HTTP)/input")
    let front0 = NSWorkspace.shared.frontmostApplication?.bundleIdentifier ?? "?"
    win.makeFirstResponder(wv)
    sendClick(win, wv, x: 250, y: 120, direct: direct)      // the text field
    await pause(0.3)
    sendKey(win, wv, "a", code: 0, direct: direct)
    sendKey(win, wv, "b", code: 11, direct: direct)
    await pause(0.3)
    sendClick(win, wv, x: 160, y: 220, direct: direct)      // the button
    await pause(0.5)
    let front1 = NSWorkspace.shared.frontmostApplication?.bundleIdentifier ?? "?"
    let log = await js(wv, "return window.__log.map(e => `${e.type}:${e.trusted ? 'T' : 'f'}:${e.target}`).join(' ');")
    let value = await js(wv, "return document.getElementById('t').value;")
    out("input placement=\(placement) direct=\(direct) value=\(value) appActive=\(NSApp.isActive) front=\(front0)->\(front1)\n   events: \(log)")
    win.orderOut(nil)
}

@MainActor func visTest(placement: String, noOcclusion: Bool) async {
    let (win, wv) = makeView(WKWebsiteDataStore(forIdentifier: ID_A))
    var applied = "n/a"
    if noOcclusion {
        let sel = Selector(("_setWindowOcclusionDetectionEnabled:"))
        if wv.responds(to: sel) { wv.setValue(false, forKey: "windowOcclusionDetectionEnabled"); applied = "off" } else { applied = "unsupported" }
    }
    switch placement {
    case "behind": win.setFrameOrigin(NSPoint(x: 80, y: 80)); win.orderBack(nil)
    case "offscreen": win.setFrameOrigin(NSPoint(x: -32000, y: -32000)); win.orderBack(nil)
    case "minimized": win.setFrameOrigin(NSPoint(x: 80, y: 80)); win.orderBack(nil); win.miniaturize(nil)
    default: break
    }
    await load(wv, "\(HTTP)/vis?tag=\(placement)-occl_\(applied)", settle: 4.5)
    win.orderOut(nil)
}

@MainActor func mainPhase(lan: String) async {
    out("safari version for UA: \(safariVersion())")
    // 1. proxies
    let p = WKWebsiteDataStore(forIdentifier: ID_P)
    p.proxyConfigurations = [ProxyConfiguration(socksv5Proxy: NWEndpoint.hostPort(host: "127.0.0.1", port: 47803))]
    let q = WKWebsiteDataStore(forIdentifier: ID_Q)
    var qc = ProxyConfiguration(socksv5Proxy: NWEndpoint.hostPort(host: "127.0.0.1", port: 47804))
    qc.applyCredential(username: "u", password: "p")
    q.proxyConfigurations = [qc]
    let (_, vp) = makeView(p)
    let (_, vq) = makeView(q)
    await load(vp, "http://probe.invalid:47801/proxy-page?tag=P", settle: 2)
    await load(vq, "http://probe-auth.invalid:47801/proxy-page?tag=Q", settle: 2)
    await load(vp, "http://127.0.0.1:47801/proxy-page?tag=P-loopback", settle: 2)
    out("proxy P title/url: \(vp.url?.absoluteString ?? "nil")")
    // 2. WebRTC: proxied store and direct store
    await load(vp, "http://probe.invalid:47801/rtc?tag=P&stun=\(lan):47805", settle: 7)
    let (_, va) = makeView(WKWebsiteDataStore(forIdentifier: ID_A))
    await load(va, "\(HTTP)/rtc?tag=A-direct&stun=\(lan):47805", settle: 7)
    // 3. input
    for placement in ["behind", "offscreen", "nowindow"] {
        for direct in [false, true] { await inputTest(placement: placement, direct: direct) }
    }
    // 4. dialogs
    await load(va, "\(HTTP)/dialog", settle: 2)
    out("dialogs answered: \(dialogs.log)")
    // 5. visibility / throttling
    for placement in ["behind", "offscreen", "minimized", "nowindow"] {
        await visTest(placement: placement, noOcclusion: false)
        await visTest(placement: placement, noOcclusion: true)
    }
    // 6. fingerprint (Safari UA)
    await load(va, "\(HTTP)/fp?src=wkwebview", settle: 3)
    // 7. isolated world + cross-origin frame evaluation
    let collector = FrameCollector()
    let (fwin, fv) = makeView(WKWebsiteDataStore(forIdentifier: ID_A), collector: collector)
    fwin.setFrameOrigin(NSPoint(x: -32000, y: -32000)); fwin.orderBack(nil)
    await load(fv, "\(HTTP)/frames", settle: 2)
    out("isolated: set secret -> \(await js(fv, "window.__ppSecret = 42; return typeof window.webkit;", world: ppWorld))")
    out("page world sees: \(await js(fv, "return [typeof window.__ppSecret, typeof window.webkit];"))")
    out("isolated await: \(await js(fv, "await new Promise(r => setTimeout(r, 100)); return 7;", world: ppWorld))")
    out("frames registered: \(collector.frames.count) -> \(collector.frames.map { "\($0.isMainFrame ? "main" : "child") \($0.securityOrigin.host):\($0.securityOrigin.port)" })")
    for f in collector.frames where !f.isMainFrame {
        out("child frame eval: \(await js(fv, "return location.href + ' | ' + document.body.innerText;", world: ppWorld, frame: f))")
    }
    // 8. snapshots: offscreen window, and a region taller than the viewport
    fv.loadHTMLString("<body style='margin:0'><div style='height:3000px;background:linear-gradient(red,blue)'></div></body>", baseURL: nil)
    await pause(1.5)
    let cfg = WKSnapshotConfiguration()
    if let img = try? await fv.takeSnapshot(configuration: cfg) { out("snapshot viewport: \(img.size)") } else { out("snapshot viewport: FAILED") }
    let tall = WKSnapshotConfiguration(); tall.rect = NSRect(x: 0, y: 0, width: 1000, height: 3000)
    if let img = try? await fv.takeSnapshot(configuration: tall) {
        let rep = img.representations.first
        out("snapshot tall rect: \(img.size) pixels=\(rep.map { "\($0.pixelsWide)x\($0.pixelsHigh)" } ?? "?")")
    } else { out("snapshot tall rect: FAILED") }
}


@MainActor func setFeature(_ prefs: WKPreferences, _ key: String, _ on: Bool) {
    let list = (WKPreferences.self as AnyObject).perform(NSSelectorFromString("_features"))?.takeUnretainedValue() as? [NSObject] ?? []
    guard let f = list.first(where: { ($0.value(forKey: "key") as? String) == key }) else { out("feature \(key) missing"); return }
    let sel = NSSelectorFromString("_setEnabled:forFeature:")
    typealias Fn = @convention(c) (AnyObject, Selector, Bool, AnyObject) -> Void
    unsafeBitCast(prefs.method(for: sel), to: Fn.self)(prefs, sel, on, f)
}

@MainActor func makeView2(_ store: WKWebsiteDataStore, rtc: Bool) -> (NSWindow, WKWebView) {
    let cfg = WKWebViewConfiguration()
    cfg.websiteDataStore = store
    cfg.applicationNameForUserAgent = "Version/\(safariVersion()) Safari/605.1.15"
    let prefs = WKPreferences()
    setFeature(prefs, "MediaDevicesEnabled", true)
    setFeature(prefs, "PushAPIEnabled", true)
    setFeature(prefs, "ApplePayEnabled", true)
    setFeature(prefs, "PeerConnectionEnabled", rtc)
    cfg.preferences = prefs
    let rect = NSRect(x: 0, y: 0, width: 1000, height: 700)
    let wv = WKWebView(frame: rect, configuration: cfg)
    wv.uiDelegate = dialogs
    let win = NSWindow(contentRect: rect, styleMask: [.titled, .closable, .resizable, .miniaturizable], backing: .buffered, defer: false)
    win.isReleasedWhenClosed = false
    win.contentView = wv
    win.setFrameOrigin(NSPoint(x: -32000, y: -32000)); win.orderBack(nil)
    return (win, wv)
}

@MainActor func phase2(lan: String) async {
    let (_, v) = makeView2(WKWebsiteDataStore(forIdentifier: ID_B), rtc: true)
    await load(v, "\(HTTP)/fp?src=wk2", settle: 3)
    out("outer/screen: \(await js(v, "return [outerWidth, outerHeight, innerWidth, innerHeight, screenX, screenY];"))  window.frame=\(v.window?.frame ?? .zero)")
    let p = WKWebsiteDataStore(forIdentifier: ID_P)
    p.proxyConfigurations = [ProxyConfiguration(socksv5Proxy: NWEndpoint.hostPort(host: "127.0.0.1", port: 47803))]
    let (_, vp) = makeView2(p, rtc: false)
    await load(vp, "http://probe.invalid:47801/rtc?tag=P-rtc-off&stun=\(lan):47805", settle: 7)
}

final class FreeWindow: NSWindow {
    override func constrainFrameRect(_ frameRect: NSRect, to screen: NSScreen?) -> NSRect { frameRect }
}

@MainActor func offscreenPhase() async {
    let rect = NSRect(x: 0, y: 0, width: 1000, height: 700)
    let cfg = WKWebViewConfiguration()
    cfg.websiteDataStore = WKWebsiteDataStore(forIdentifier: ID_B)
    cfg.applicationNameForUserAgent = "Version/\(safariVersion()) Safari/605.1.15"
    let wv = WKWebView(frame: rect, configuration: cfg)
    wv.uiDelegate = dialogs
    let win = FreeWindow(contentRect: rect, styleMask: [.titled, .closable, .resizable, .miniaturizable], backing: .buffered, defer: false)
    win.isReleasedWhenClosed = false
    win.contentView = wv
    let occlArg = CommandLine.arguments.count > 2 && CommandLine.arguments[2] == "noocclusion"
    if occlArg && wv.responds(to: Selector(("_setWindowOcclusionDetectionEnabled:"))) { wv.setValue(false, forKey: "windowOcclusionDetectionEnabled"); out("occlusion detection: off") }
    win.orderBack(nil)
    win.setFrameOrigin(NSPoint(x: -32000, y: -32000))
    out("offscreen frame after order-in: \(win.frame) onActiveSpace=\(win.isOnActiveSpace) visible=\(win.isVisible) occlusion=\(win.occlusionState.contains(.visible))")
    await load(wv, "\(HTTP)/vis?tag=true-offscreen-\(occlArg ? "noocclusion" : "default")", settle: 4.5)
    win.makeFirstResponder(wv)
    await load(wv, "\(HTTP)/input", settle: 0.5)
    sendClick(win, wv, x: 250, y: 120, direct: true)
    await pause(0.2)
    sendKey(win, wv, "z", code: 6, direct: true)
    await pause(0.3)
    out("true-offscreen input: value=\(await js(wv, "return document.getElementById('t').value;")) events=\(await js(wv, "return window.__log.map(e => `${e.type}:${e.trusted ? 'T' : 'f'}`).join(' ');"))")
    if let img = try? await wv.takeSnapshot(configuration: WKSnapshotConfiguration()) { out("true-offscreen snapshot: \(img.size)") } else { out("true-offscreen snapshot: FAILED") }
    out("frame at end: \(win.frame)")
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ note: Notification) {
        Task { @MainActor in
            let args = CommandLine.arguments
            switch args.count > 1 ? args[1] : "" {
            case "persist1": await persist1()
            case "persist2": await persist2()
            case "offscreen": await offscreenPhase()
            case "phase2": await phase2(lan: args.count > 2 ? args[2] : "127.0.0.1")
            case "main": await mainPhase(lan: args.count > 2 ? args[2] : "127.0.0.1")
            default: out("usage: probe persist1|persist2|main <lan-ip>")
            }
            out("done")
            exit(0)
        }
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let delegate = AppDelegate()
app.delegate = delegate
app.run()
