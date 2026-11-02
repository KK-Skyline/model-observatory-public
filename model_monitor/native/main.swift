import AppKit
import Foundation
import UniformTypeIdentifiers
import WebKit

enum HostError: LocalizedError {
    case message(String)
    var errorDescription: String? { if case let .message(text) = self { return text }; return nil }
}

struct LocalSession: Decodable {
    let port: Int
    let token: String
    let pid: Int
    var origin: URL { URL(string: "http://127.0.0.1:\(port)")! }
}

@MainActor
final class Backend {
    var ownedProcess: Process?
    var stderrPipe: Pipe?
    let client = URLSession(configuration: .ephemeral)

    func connect(project: URL) async throws -> LocalSession {
        let source = project.appendingPathComponent("model_monitor/design_preview/preview.py")
        guard FileManager.default.fileExists(atPath: source.path) else {
            throw HostError.message("没有找到 model_monitor/design_preview/preview.py。请选择恢复后的项目文件夹。")
        }
        let state = project.appendingPathComponent("model_monitor/design_preview/data/session.json")
        if let session = try? await readSession(state) { return session }
        if let process = ownedProcess, process.isRunning {
            throw HostError.message("本应用启动的后端尚未就绪。请稍后重试。")
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        process.arguments = ["-m", "model_monitor.design_preview.preview", "--port", "18085"]
        process.currentDirectoryURL = project
        process.standardOutput = FileHandle.nullDevice
        let errors = Pipe()
        process.standardError = errors
        try process.run()
        ownedProcess = process
        stderrPipe = errors
        for _ in 0..<60 {
            guard process.isRunning else {
                throw HostError.message("Python 后端启动失败（退出码 \(process.terminationStatus)）。请检查项目完整性，以及 18085 端口是否已被其他进程占用。")
            }
            if let session = try? await readSession(state), session.pid == Int(process.processIdentifier) { return session }
            try await Task.sleep(nanoseconds: 150_000_000)
        }
        process.terminate()
        throw HostError.message("本机后端在 12 秒内未完成鉴权连接。没有停止其他服务；请检查项目目录和 18085 端口后重试。")
    }

    func readSession(_ state: URL) async throws -> LocalSession {
        let session = try JSONDecoder().decode(LocalSession.self, from: Data(contentsOf: state))
        guard session.port == 18085, session.token.count >= 32, session.pid > 0 else {
            throw HostError.message("本地会话字段无效；拒绝连接非预览端口。")
        }
        var request = URLRequest(url: session.origin.appendingPathComponent("api/settings"))
        request.timeoutInterval = 1
        request.setValue("Bearer " + session.token, forHTTPHeaderField: "Authorization")
        let (_, response) = try await client.data(for: request)
        guard (response as? HTTPURLResponse)?.statusCode == 200 else {
            throw HostError.message("本机后端鉴权未通过。")
        }
        return session
    }

    func stopOwned() {
        if let process = ownedProcess, process.isRunning { process.terminate() }
        ownedProcess = nil
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate {
    let backend = Backend()
    var window: NSWindow!
    var webView: WKWebView!
    var project: URL?
    var session: LocalSession?
    var connecting = false
    var testStarted = false
    var status: NSTextField!
    let arguments = CommandLine.arguments

    func option(_ key: String) -> String? {
        guard let index = arguments.firstIndex(of: key), arguments.indices.contains(index + 1) else { return nil }
        return arguments[index + 1]
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        makeMenu()
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1280, height: 800),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "模型核对"
        window.minSize = NSSize(width: 860, height: 600)
        window.isReleasedWhenClosed = false
        window.delegate = self
        window.collectionBehavior = [.fullScreenPrimary]
        window.appearance = NSAppearance(named: .aqua)
        window.titlebarAppearsTransparent = true
        window.backgroundColor = NSColor.windowBackgroundColor
        NSApp.applicationIconImage = NSImage(systemSymbolName: "checkmark.shield", accessibilityDescription: "模型核对")
        window.center()
        showLoading("正在连接本机模型核对面板…")
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        if let path = option("--project") {
            project = URL(fileURLWithPath: path, isDirectory: true)
        } else if let file = Bundle.main.url(forResource: "project-path", withExtension: "txt"),
                  let path = try? String(contentsOf: file, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines) {
            project = URL(fileURLWithPath: path, isDirectory: true)
        }
        Task { await connect() }
    }

    func makeMenu() {
        let menu = NSMenu()
        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "关于模型核对", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "退出模型核对", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        menu.addItem(appItem)
        let file = NSMenu(title: "文件")
        file.addItem(withTitle: "显示窗口", action: #selector(showWindow), keyEquivalent: "0").target = self
        file.addItem(withTitle: "选择项目文件夹…", action: #selector(chooseProject), keyEquivalent: "o").target = self
        file.addItem(withTitle: "刷新面板", action: #selector(reloadPanel), keyEquivalent: "r").target = self
        file.addItem(.separator())
        file.addItem(withTitle: "关闭窗口", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        let fileItem = NSMenuItem(title: "文件", action: nil, keyEquivalent: "")
        fileItem.submenu = file
        menu.addItem(fileItem)
        let edit = NSMenu(title: "编辑")
        for (title, action, key) in [("撤销", "undo:", "z"), ("剪切", "cut:", "x"), ("拷贝", "copy:", "c"), ("粘贴", "paste:", "v"), ("全选", "selectAll:", "a")] {
            edit.addItem(withTitle: title, action: Selector(action), keyEquivalent: key)
        }
        let editItem = NSMenuItem(title: "编辑", action: nil, keyEquivalent: "")
        editItem.submenu = edit
        menu.addItem(editItem)
        let windows = NSMenu(title: "窗口")
        windows.addItem(withTitle: "最小化", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        windows.addItem(withTitle: "缩放", action: #selector(NSWindow.performZoom(_:)), keyEquivalent: "")
        let full = windows.addItem(withTitle: "进入或退出全屏", action: #selector(NSWindow.toggleFullScreen(_:)), keyEquivalent: "f")
        full.keyEquivalentModifierMask = [.control, .command]
        let windowItem = NSMenuItem(title: "窗口", action: nil, keyEquivalent: "")
        windowItem.submenu = windows
        menu.addItem(windowItem)
        NSApp.windowsMenu = windows
        NSApp.mainMenu = menu
    }

    func showLoading(_ message: String) {
        let view = NSView(frame: NSRect(x: 0, y: 0, width: 1280, height: 800))
        status = NSTextField(wrappingLabelWithString: message)
        status.font = .systemFont(ofSize: 15)
        status.alignment = .center
        status.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(status)
        let retry = NSButton(title: "重新连接", target: self, action: #selector(reloadPanel))
        retry.bezelStyle = .rounded
        retry.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(retry)
        let choose = NSButton(title: "选择项目文件夹…", target: self, action: #selector(chooseProject))
        choose.bezelStyle = .rounded
        choose.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(choose)
        NSLayoutConstraint.activate([
            status.centerXAnchor.constraint(equalTo: view.centerXAnchor), status.centerYAnchor.constraint(equalTo: view.centerYAnchor, constant: -25),
            status.widthAnchor.constraint(lessThanOrEqualToConstant: 600),
            retry.centerXAnchor.constraint(equalTo: view.centerXAnchor, constant: -80), retry.topAnchor.constraint(equalTo: status.bottomAnchor, constant: 22),
            choose.leadingAnchor.constraint(equalTo: retry.trailingAnchor, constant: 16), choose.centerYAnchor.constraint(equalTo: retry.centerYAnchor)
        ])
        window.contentView = view
    }

    func connect() async {
        guard !connecting else { return }
        connecting = true
        defer { connecting = false }
        do {
            guard let project = project else { throw HostError.message("请选择包含 model_monitor 的项目文件夹。") }
            session = try await backend.connect(project: project)
            let configuration = WKWebViewConfiguration()
            configuration.websiteDataStore = .nonPersistent()
            configuration.userContentController.addUserScript(WKUserScript(source: "window.__observatoryErrorCount=0; addEventListener('error',()=>window.__observatoryErrorCount++); addEventListener('unhandledrejection',()=>window.__observatoryErrorCount++);", injectionTime: .atDocumentStart, forMainFrameOnly: true))
            webView = WKWebView(frame: window.contentView!.bounds, configuration: configuration)
            webView.autoresizingMask = [.width, .height]
            webView.navigationDelegate = self
            webView.uiDelegate = self
            window.contentView = webView
            var parts = URLComponents(url: session!.origin, resolvingAgainstBaseURL: false)!
            parts.path = "/"
            parts.queryItems = [URLQueryItem(name: "open", value: UUID().uuidString)]
            parts.fragment = session!.token
            // Keep the private URL in memory; the existing controller clears its fragment.
            // 私有 URL 只在内存中传递，由原控制器清除 fragment。
            webView.load(URLRequest(url: parts.url!))
        } catch {
            showLoading(error.localizedDescription)
            if let path = option("--self-test") { failTest(path, error: error) }
        }
    }

    @objc func showWindow() {
        window.deminiaturize(nil)
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc func reloadPanel() { Task { await connect() } }

    @objc func chooseProject() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.message = "选择包含 model_monitor 的项目文件夹"
        if panel.runModal() == .OK, let url = panel.url {
            project = url
            backend.stopOwned()
            Task { await connect() }
        }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showWindow()
        return true
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
    func applicationWillTerminate(_ notification: Notification) { backend.stopOwned() }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url, let session = session,
              url.scheme == "http", url.host == "127.0.0.1", url.port == session.port,
              url.user == nil, url.password == nil else { decisionHandler(.cancel); return }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        if (error as NSError).code != NSURLErrorCancelled { showLoading("页面加载失败：" + error.localizedDescription) }
    }

    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        panel.allowedContentTypes = [.png, .jpeg] + [UTType(filenameExtension: "webp")].compactMap { $0 }
        panel.beginSheetModal(for: window) { result in completionHandler(result == .OK ? panel.urls : nil) }
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        guard let path = option("--self-test"), !testStarted else { return }
        testStarted = true
        Task {
            do { try await selfTest(path) }
            catch { failTest(path, error: error) }
        }
    }

    func waitFor(_ condition: @escaping () async throws -> Bool) async throws {
        for _ in 0..<80 {
            if try await condition() { return }
            try await Task.sleep(nanoseconds: 100_000_000)
        }
        throw HostError.message("运行检查超时。")
    }

    func js(_ script: String) async throws -> Any {
        guard let result = try await webView.evaluateJavaScript(script) else {
            throw HostError.message("页面检查没有返回结果。")
        }
        return result
    }

    func require(_ condition: Bool, _ message: String) throws {
        if !condition { throw HostError.message(message) }
    }

    func selfTest(_ path: String) async throws {
        let output = URL(fileURLWithPath: path)
        var checks: [String: Bool] = [:]
        try await waitFor { (try await self.js("document.querySelector('#connection')?.textContent.startsWith('本机连接正常')")) as? Bool == true }
        checks["real_backend_connected"] = true
        try require((try await js("document.querySelectorAll('#rows tr').length")) as? Int ?? 0 > 0, "没有真实对话行。")
        checks["real_rows_visible"] = true
        try await testDemo()
        checks["synthetic_three_states"] = true
        try await testWallpaper()
        checks["wallpaper_upload_and_persistence"] = true
        checks["wallpaper_restored"] = true
        try require((try await js("window.__observatoryErrorCount === 0")) as? Bool == true, "网页存在未处理的脚本错误。")
        checks["javascript_errors_zero"] = true
        try await waitFor { (try await self.js("document.querySelector('#connection')?.textContent.startsWith('本机连接正常') && document.querySelectorAll('#rows button').length > 0")) as? Bool == true }
        let originalSize = webView.bounds.size
        window.setContentSize(NSSize(width: 860, height: 600))
        try await waitFor { (try await self.js("document.documentElement.scrollWidth <= innerWidth")) as? Bool == true }
        checks["minimum_window_no_page_overflow"] = true
        window.setContentSize(originalSize)
        for kind: NSWindow.ButtonType in [.closeButton, .miniaturizeButton, .zoomButton] {
            try require(window.standardWindowButton(kind)?.isEnabled == true, "系统窗口按钮不可用。")
        }
        checks["native_buttons_enabled"] = true
        window.standardWindowButton(.miniaturizeButton)!.performClick(nil)
        try await waitFor { self.window.isMiniaturized }
        checks["yellow_minimizes"] = true
        window.deminiaturize(nil)
        try await waitFor { !self.window.isMiniaturized && self.window.isVisible }
        checks["dock_restore"] = true
        let oldFrame = window.frame
        window.standardWindowButton(.zoomButton)!.performClick(nil)
        try await waitFor { self.window.styleMask.contains(.fullScreen) || self.window.frame != oldFrame }
        checks["green_changes_native_window"] = true
        if window.styleMask.contains(.fullScreen) {
            try await Task.sleep(nanoseconds: 1_000_000_000)
            window.toggleFullScreen(nil)
            try await waitFor { !self.window.styleMask.contains(.fullScreen) }
        } else { window.performZoom(nil) }
        checks["green_restores"] = true
        window.standardWindowButton(.closeButton)!.performClick(nil)
        try await waitFor { !self.window.isVisible }
        checks["red_closes_window"] = true
        showWindow()
        try require(window.isVisible, "关闭后无法重开窗口。")
        checks["closed_window_reopens"] = true
        let result: [String: Any] = ["result":"passed", "checks":checks,
                                  "nativeWindowNumber":window.windowNumber, "backendReused":backend.ownedProcess == nil]
        try JSONSerialization.data(withJSONObject: result, options: [.prettyPrinted, .sortedKeys]).write(to: output)
        print("Native shell runtime checks passed; private session not printed.")
        if !arguments.contains("--keep-open") { NSApp.terminate(nil) }
    }

    func testDemo() async throws {
        _ = try await js("document.querySelector('#demo').click(); true")
        try await waitFor { (try await self.js("document.querySelector('#total').textContent === '3' && !document.querySelector('#demo-banner').hidden")) as? Bool == true }
        try require((try await js("new Set([...document.querySelectorAll('#rows td:nth-child(6)')].map(e=>e.textContent)).size")) as? Int == 3, "合成三态未显示。")
        webView.load(URLRequest(url: session!.origin))
        try await waitFor { self.webView.url?.path == "/" && !self.webView.isLoading }
        try await waitFor { (try await self.js("document.querySelector('#connection')?.textContent.startsWith('本机连接正常')")) as? Bool == true }
    }

    func settings() async throws -> String {
        var request = URLRequest(url: session!.origin.appendingPathComponent("api/settings"))
        request.setValue("Bearer " + session!.token, forHTTPHeaderField: "Authorization")
        let (data, response) = try await backend.client.data(for: request)
        guard (response as? HTTPURLResponse)?.statusCode == 200,
              let object = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              let wallpaper = object["wallpaper"] as? String else { throw HostError.message("壁纸设置读取失败。") }
        return wallpaper
    }

    func testWallpaper() async throws {
        let previous = try await settings()
        let png = "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAAAlC+aJAAAAeklEQVR4nO3PUQkAIBTAwNfNPIayqSH8OITBAtxm7fN1wwUNaEEDWtCAFjSgBQ1oQQNa0IAWNKAFDWhBA1rQgBY0oAUNaEEDWtCAFjSgBQ1oQQNa0IAWNKAFDWhBA1rQgBY0oAUNaEEDWtCAFjSgBQ1oQQNa0IAWPHYBF6YRPAacYbAAAAAASUVORK5CYII="
        _ = try await js("""
        document.querySelector('#wallpaper-open').click();
        const bytes = Uint8Array.from(atob('\(png)'), c => c.charCodeAt(0));
        const transfer = new DataTransfer();
        transfer.items.add(new File([bytes], 'native-runtime-test.png', {type:'image/png'}));
        const input = document.querySelector('#wallpaper-file');
        input.files = transfer.files;
        input.dispatchEvent(new Event('change'));
        true
        """)
        try await waitFor { (try await self.js("!document.querySelector('#import-apply').disabled")) as? Bool == true }
        _ = try await js("document.querySelector('#import-apply').click(); true")
        try await waitFor { (try await self.js("document.querySelector('#wallpaper-status').textContent === '导入图片已保存并应用'")) as? Bool == true }
        let imported = try await settings()
        try require(imported.hasPrefix("import-"), "导入壁纸未持久保存。")
        _ = try await js("document.querySelector('#wallpaper-close').click(); true")
        webView.reload()
        try await waitFor { !self.webView.isLoading }
        let encoded = String(data: try JSONSerialization.data(withJSONObject: [imported]), encoding: .utf8)!
        try await waitFor { (try await self.js("document.body.dataset.wallpaper === \(encoded)[0]")) as? Bool == true }
        let old = String(data: try JSONSerialization.data(withJSONObject: [previous]), encoding: .utf8)!
        _ = try await js("applyWallpaper(\(old)[0]); true")
        try await waitFor { try await self.settings() == previous }
    }

    func failTest(_ path: String, error: Error) {
        let result = ["result":"blocked", "error":error.localizedDescription]
        do { try JSONSerialization.data(withJSONObject: result, options: [.prettyPrinted]).write(to: URL(fileURLWithPath: path)) }
        catch { fputs("Cannot write native runtime report.\n", stderr) }
        fputs("Native runtime check failed: \(error.localizedDescription)\n", stderr)
        backend.stopOwned()
        exit(1)
    }
}

MainActor.assumeIsolated {
    let application = NSApplication.shared
    let delegate = AppDelegate()
    application.delegate = delegate
    withExtendedLifetime(delegate) { application.run() }
}
