import Cocoa
import Darwin
import Foundation
import WebKit

final class HealthResult: @unchecked Sendable {
    private let lock = NSLock()
    private var value = false
    func set(_ flag: Bool) { lock.lock(); value = flag; lock.unlock() }
    func get() -> Bool { lock.lock(); defer { lock.unlock() }; return value }
}

final class DropWebView: WKWebView {
    var onFolder: ((String) -> Void)?

    override init(frame frameRect: NSRect, configuration: WKWebViewConfiguration) {
        super.init(frame: frameRect, configuration: configuration)
        registerForDraggedTypes([.fileURL])
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    override func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation { .copy }

    override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
        guard let urls = sender.draggingPasteboard.readObjects(forClasses: [NSURL.self], options: nil) as? [URL] else { return false }
        guard let folder = urls.first(where: { $0.hasDirectoryPath }) else { return false }
        onFolder?(folder.path)
        return true
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
    private var window: NSWindow!
    private var webView: DropWebView!
    private var process: Process?
    private var logHandle: FileHandle?
    private var opening = false
    private var port = 8777
    private var base: String { "http://127.0.0.1:\(port)" }
    private let bundleIdentifier = "com.aipieksel.dev-app-dropper"

    private func preferredPort() -> Int {
        let value = Int(ProcessInfo.processInfo.environment["CONVERTO_PORT"] ?? "8777") ?? 8777
        return (1...65535).contains(value) ? value : 8777
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        if let existing = NSRunningApplication.runningApplications(withBundleIdentifier: bundleIdentifier).first(where: { $0.processIdentifier != getpid() && !$0.isTerminated }) {
            existing.activate(options: [.activateAllWindows]); NSApp.terminate(nil); return
        }
        NSApp.setActivationPolicy(.regular)
        makeMenu()
        let config = WKWebViewConfiguration()
        config.userContentController.add(self, name: "appDropper")
        webView = DropWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.onFolder = { [weak self] path in self?.deliver(path) }
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1100, height: 760), styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "Dev App Dropper and Launcher"
        window.minSize = NSSize(width: 760, height: 560)
        window.contentView = webView
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        start()
    }

    func application(_ sender: NSApplication, openFile filename: String) -> Bool {
        var isDirectory: ObjCBool = false
        guard FileManager.default.fileExists(atPath: filename, isDirectory: &isDirectory), isDirectory.boolValue else { return false }
        deliver(filename)
        return true
    }

    func application(_ application: NSApplication, open urls: [URL]) {
        for url in urls where url.hasDirectoryPath { deliver(url.path) }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true); return true
    }
    func applicationWillTerminate(_ notification: Notification) {
        if let process = process, process.isRunning { process.terminate() }
        try? logHandle?.close()
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        if message.name == "appDropper" { chooseFolder() }
    }

    private func chooseFolder() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.prompt = "Create Mac app"
        panel.message = "Choose a project folder. App Dropper builds a Mac app in App Dropper Apps and the Apps folder."
        guard panel.runModal() == .OK, let url = panel.url else { return }
        deliver(url.path)
    }

    private func deliver(_ path: String) {
        guard let data = try? JSONEncoder().encode(path), let literal = String(data: data, encoding: .utf8) else { return }
        DispatchQueue.main.async {
            self.webView.evaluateJavaScript("window.receiveFolderPath(\(literal))", completionHandler: nil)
        }
    }

    private func makeMenu() {
        let menu = NSMenu()
        let appItem = NSMenuItem(); menu.addItem(appItem)
        let app = NSMenu(); appItem.submenu = app
        app.addItem(withTitle: "About App Dropper", action: #selector(about), keyEquivalent: "")
        app.addItem(.separator())
        app.addItem(withTitle: "Quit App Dropper", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        let editItem = NSMenuItem(title: "Edit", action: nil, keyEquivalent: ""); menu.addItem(editItem)
        let edit = NSMenu(title: "Edit"); editItem.submenu = edit
        for (title, action, key) in [("Undo", "undo:", "z"), ("Cut", "cut:", "x"), ("Copy", "copy:", "c"), ("Paste", "paste:", "v"), ("Select All", "selectAll:", "a")] {
            edit.addItem(withTitle: title, action: Selector(action), keyEquivalent: key)
        }
        let helpItem = NSMenuItem(title: "Help", action: nil, keyEquivalent: ""); menu.addItem(helpItem)
        let help = NSMenu(title: "Help"); helpItem.submenu = help
        help.addItem(withTitle: "Retry App Startup", action: #selector(retry), keyEquivalent: "r")
        NSApp.mainMenu = menu
    }

    @objc private func about() {
        let alert = NSAlert()
        alert.messageText = "App Dropper 1.0.0"
        alert.informativeText = "Drop a project folder. App Dropper builds a Mac app in App Dropper Apps and in the Apps folder."
        alert.runModal()
    }

    @objc private func retry() { if !opening { start() } }

    private func escape(_ text: String) -> String {
        text.replacingOccurrences(of: "&", with: "&amp;").replacingOccurrences(of: "<", with: "&lt;").replacingOccurrences(of: ">", with: "&gt;")
    }

    private func showStatus(_ title: String, _ detail: String) {
        DispatchQueue.main.async {
            self.webView.loadHTMLString("<html><body style='margin:0;background:#000000;color:#f4f4f4;font:16px -apple-system;padding:70px'><div style='max-width:650px'><p style='color:#10a37f'>APP DROPPER</p><h1>\(self.escape(title))</h1><p style='line-height:1.8'>\(self.escape(detail))</p></div></body></html>", baseURL: nil)
        }
    }

    private func tcpListening(_ candidate: Int) -> Bool {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        if fd < 0 { return false }
        defer { close(fd) }
        var timeout = timeval(tv_sec: 0, tv_usec: 200_000)
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))
        var addr = sockaddr_in()
        addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = in_port_t(UInt16(candidate)).bigEndian
        addr.sin_addr = in_addr(s_addr: inet_addr("127.0.0.1"))
        let connected = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.connect(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        return connected == 0
    }

    private func ownServerPort() -> Int? {
        let start = preferredPort()
        for candidate in start..<(start + 51) where tcpListening(candidate) {
            port = candidate
            if healthy() { return candidate }
        }
        port = start
        return nil
    }

    private func readyPort(in log: URL) -> Int? {
        guard let text = try? String(contentsOf: log, encoding: .utf8) else { return nil }
        let marker = "App Dropper ready on http://127.0.0.1:"
        guard let range = text.range(of: marker, options: .backwards) else { return nil }
        let digits = text[range.upperBound...].prefix { $0.isNumber }
        return Int(digits)
    }

    private func logDetail(_ log: URL) -> String {
        let text = (try? String(contentsOf: log, encoding: .utf8)) ?? ""
        let tail = text.split(separator: "\n", omittingEmptySubsequences: false).suffix(12).joined(separator: "\n")
        let trimmed = tail.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty { return "No startup log was written at \(log.path)." }
        return trimmed
    }

    private func pythonAppPath() -> String? {
        guard let resources = Bundle.main.resourceURL else { return nil }
        let app = resources.appendingPathComponent("app")
        guard FileManager.default.fileExists(atPath: app.appendingPathComponent("entry").path) else { return nil }
        return app.path
    }

    private func healthy() -> Bool {
        let request = URLRequest(url: URL(string: base + "/api/health")!, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 1)
        let semaphore = DispatchSemaphore(value: 0)
        let ok = HealthResult()
        URLSession.shared.dataTask(with: request) { data, response, _ in
            defer { semaphore.signal() }
            if let data = data, (response as? HTTPURLResponse)?.statusCode == 200,
               let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                ok.set(json["app"] as? String == "app-dropper")
            }
        }.resume()
        _ = semaphore.wait(timeout: .now() + 2)
        return ok.get()
    }

    private func pythonPath() -> String? {
        if let runtime = UserDefaults.standard.string(forKey: "pythonRuntime"), FileManager.default.isExecutableFile(atPath: runtime) {
            return runtime
        }
        if let bundled = Bundle.main.resourceURL?.appendingPathComponent("runtime.json"),
           let data = try? Data(contentsOf: bundled),
           let config = try? JSONSerialization.jsonObject(with: data) as? [String: String],
           let python = config["python"], FileManager.default.isExecutableFile(atPath: python) {
            return python
        }
        return ["/Library/Frameworks/Python.framework/Versions/3.12/bin/python3", "/opt/homebrew/bin/python3", "/usr/bin/python3"].first { FileManager.default.isExecutableFile(atPath: $0) }
    }

    private func start() {
        opening = true
        showStatus("Opening App Dropper…", "Starting the folder dropper on this Mac.")
        DispatchQueue.global(qos: .userInitiated).async {
            defer { DispatchQueue.main.async { self.opening = false } }
            if let running = self.ownServerPort() {
                self.port = running
                self.loadApp()
                return
            }
            if let old = self.process, old.isRunning {
                self.showStatus("The app is still starting", "Wait a moment, then use Retry App Startup.")
                return
            }
            guard let python = self.pythonPath() else {
                self.showStatus("Python is missing", "Install Python 3, then retry.")
                return
            }
            guard let appPath = self.pythonAppPath() else {
                self.showStatus("Open Dev App Dropper and Launcher.app", "This file is outside its app bundle, so it cannot load the dropper.")
                return
            }
            do {
                let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent("App Dropper")
                try FileManager.default.createDirectory(at: support, withIntermediateDirectories: true)
                let log = support.appendingPathComponent("runtime.log")
                FileManager.default.createFile(atPath: log.path, contents: nil)
                let handle = try FileHandle(forWritingTo: log)
                try handle.truncate(atOffset: 0)
                self.logHandle = handle
                let process = Process()
                process.executableURL = URL(fileURLWithPath: python)
                process.arguments = ["-c", "import runpy; runpy.run_module('entry', run_name='__main__')"]
                var env = ProcessInfo.processInfo.environment
                env["PYTHONPATH"] = appPath
                env["PYTHONUNBUFFERED"] = "1"
                env["PYTHONDONTWRITEBYTECODE"] = "1"
                env["CONVERTO_PORT"] = String(self.preferredPort())
                env["APP_DROPPER_DATA_DIR"] = support.path
                process.environment = env
                if let resources = Bundle.main.resourceURL { process.currentDirectoryURL = resources }
                process.standardOutput = handle
                process.standardError = handle
                try process.run()
                self.process = process
                for _ in 0..<120 {
                    if let bound = self.readyPort(in: log) { self.port = bound }
                    if self.healthy() { self.loadApp(); return }
                    if !process.isRunning { break }
                    Thread.sleep(forTimeInterval: 0.25)
                }
                self.showStatus("App Dropper could not start", self.logDetail(log))
            } catch {
                self.showStatus("Startup needs attention", error.localizedDescription)
            }
        }
    }

    private func loadApp() {
        DispatchQueue.main.async {
            self.webView.load(URLRequest(url: URL(string: self.base)!, cachePolicy: .reloadIgnoringLocalCacheData))
        }
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else { decisionHandler(.cancel); return }
        if url.scheme == "about" { decisionHandler(.allow); return }
        if url.scheme == "http", url.host == "127.0.0.1", url.port == port {
            decisionHandler(.allow); return
        }
        if ["https", "http", "mailto"].contains(url.scheme ?? "") { NSWorkspace.shared.open(url) }
        decisionHandler(.cancel)
    }
}

if CommandLine.arguments.contains("--quit-running") {
    let others = NSRunningApplication.runningApplications(withBundleIdentifier: "com.aipieksel.dev-app-dropper").filter { $0.processIdentifier != getpid() }
    for existing in others { existing.terminate() }
    for _ in 0..<50 {
        if others.allSatisfy({ $0.isTerminated }) { exit(0) }
        RunLoop.current.run(until: Date().addingTimeInterval(0.1))
    }
    fputs("Quit App Dropper before rebuilding.\n", stderr)
    exit(1)
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
