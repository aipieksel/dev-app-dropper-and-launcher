import Cocoa
import Darwin
import Foundation

final class LauncherDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        _ = runLaunch()
        NSApp.activate(ignoringOtherApps: true)
        return true
    }
}

func pythonBinary() -> String? {
    let candidates = ["/usr/bin/python3", "/opt/homebrew/bin/python3", "/usr/local/bin/python3"]
    return candidates.first { FileManager.default.isExecutableFile(atPath: $0) }
}

func runLaunch() -> Int32 {
    fputs("entrypoint: starting\n", stdout)
    fflush(stdout)
    guard let resources = Bundle.main.resourceURL else {
        fputs("error: This launcher is missing its bundle resources.\n", stderr)
        return 2
    }
    let contract = resources.appendingPathComponent("launch-contract.json")
    let runtime = resources.appendingPathComponent("launcher_runtime.py")
    guard FileManager.default.isReadableFile(atPath: contract.path),
          FileManager.default.isReadableFile(atPath: runtime.path) else {
        fputs("error: This launcher is missing its start contract.\n", stderr)
        return 2
    }
    guard let python = pythonBinary() else {
        fputs("error: Python 3 is not installed.\n", stderr)
        return 2
    }
    let task = Process()
    task.executableURL = URL(fileURLWithPath: python)
    task.arguments = ["-u", runtime.path, "launch", contract.path]
    var environment = ProcessInfo.processInfo.environment
    let path = environment["PATH"] ?? ""
    environment["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:" + path
    environment["PYTHONUNBUFFERED"] = "1"
    task.environment = environment
    task.standardOutput = FileHandle.standardOutput
    task.standardError = FileHandle.standardError
    do {
        try task.run()
    } catch {
        fputs("error: \(error.localizedDescription)\n", stderr)
        return 2
    }
    task.waitUntilExit()
    return task.terminationStatus
}

func installMenu() {
    let menu = NSMenu()
    let appItem = NSMenuItem()
    menu.addItem(appItem)
    let appMenu = NSMenu()
    appItem.submenu = appMenu
    let name = Bundle.main.object(forInfoDictionaryKey: "CFBundleName") as? String ?? "App"
    appMenu.addItem(withTitle: "Quit \(name)", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
    NSApp.mainMenu = menu
}

let status = runLaunch()
if status != 0 {
    fputs("entrypoint: failed\n", stderr)
    fflush(stderr)
    exit(status)
}

let app = NSApplication.shared
let delegate = LauncherDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
installMenu()
app.run()
