import AppKit
import Foundation
import Security

let appName = "Immich Card Sync"
let fm = FileManager.default
let support = fm.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/Immich Card Sync")
let stateFolder = support.appendingPathComponent("state")
let configURL = support.appendingPathComponent("config.json")
let resourceKeys: Set<URLResourceKey> = [.volumeIsLocalKey, .volumeIsRemovableKey, .volumeIsEjectableKey, .volumeUUIDStringKey]

func cameraCard(_ url: URL) -> Bool {
    guard url.deletingLastPathComponent().path == "/Volumes",
          let values = try? url.resourceValues(forKeys: resourceKeys),
          values.volumeIsLocal == true,
          values.volumeIsRemovable == true || values.volumeIsEjectable == true else { return false }
    return ["DCIM", "MP_ROOT", "PRIVATE/M4ROOT", "PRIVATE/AVCHD"].contains { marker in
        var isDirectory: ObjCBool = false
        return fm.fileExists(atPath: url.appendingPathComponent(marker).path, isDirectory: &isDirectory) && isDirectory.boolValue
    }
}

func volumeIdentity(_ url: URL) -> String {
    (try? url.resourceValues(forKeys: resourceKeys))?.volumeUUIDString ?? url.path
}

func listCards() -> [URL] {
    (fm.mountedVolumeURLs(includingResourceValuesForKeys: Array(resourceKeys), options: []) ?? []).filter(cameraCard)
}

// Configuration and credentials are supplied during installation, never compiled into releases.
func configuration() -> [String: Any] {
    guard let data = try? Data(contentsOf: configURL),
          let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return [:] }
    return value
}
func storeKey(service: String, account: String, data: Data) throws {
    let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
        kSecAttrService as String: service, kSecAttrAccount as String: account]
    var trusted: SecTrustedApplication?
    guard SecTrustedApplicationCreateFromPath("/usr/bin/security", &trusted) == errSecSuccess,
          let trusted = trusted else { throw NSError(domain: appName, code: 1) }
    var access: SecAccess?
    guard SecAccessCreate(appName as CFString, [trusted] as CFArray, &access) == errSecSuccess,
          let access = access else { throw NSError(domain: appName, code: 2) }
    var attributes = query
    attributes[kSecValueData as String] = data
    attributes[kSecAttrAccess as String] = access
    let status = SecItemAdd(attributes as CFDictionary, nil)
    if status == errSecDuplicateItem {
        let update = SecItemUpdate(query as CFDictionary, [kSecValueData as String: data] as CFDictionary)
        guard update == errSecSuccess else { throw NSError(domain: NSOSStatusErrorDomain, code: Int(update)) }
    } else if status != errSecSuccess { throw NSError(domain: NSOSStatusErrorDomain, code: Int(status)) }
}
if CommandLine.arguments.count == 4 && CommandLine.arguments[1] == "--store-key-stdin" {
    do {
        let data = FileHandle.standardInput.readDataToEndOfFile()
        guard !data.isEmpty else { throw NSError(domain: appName, code: 3) }
        try storeKey(service: CommandLine.arguments[2], account: CommandLine.arguments[3], data: data)
        print("Credential saved to Keychain.")
        exit(0)
    } catch { fputs("Could not save the credential to Keychain.\n", stderr); exit(1) }
}
if CommandLine.arguments.contains("--list-cards") {
    let result = listCards().map { ["path": $0.path, "uuid": volumeIdentity($0)] }
    print(String(data: try! JSONSerialization.data(withJSONObject: result), encoding: .utf8)!)
    exit(0)
}

if CommandLine.arguments.contains("--observe-card-mount") {
    var observed = false
    let observer = NSWorkspace.shared.notificationCenter.addObserver(forName: NSWorkspace.didMountNotification, object: nil, queue: .main) { notification in
        if let url = notification.userInfo?[NSWorkspace.volumeURLUserInfoKey] as? URL {
            let isCard = cameraCard(url)
            let row: [String: Any] = ["path":url.path, "cameraCard":isCard, "uuid":volumeIdentity(url)]
            print(String(data: try! JSONSerialization.data(withJSONObject: row), encoding: .utf8)!)
            fflush(stdout)
            if isCard { observed = true }
        }
    }
    let deadline = Date().addingTimeInterval(30)
    while !observed && Date() < deadline { RunLoop.main.run(until: Date().addingTimeInterval(0.1)) }
    NSWorkspace.shared.notificationCenter.removeObserver(observer)
    exit(observed ? 0 : 1)
}

final class CardSync: NSObject, NSApplicationDelegate {
    var statusItem: NSStatusItem!
    var statusLine: NSMenuItem!
    var pauseItem: NSMenuItem!
    var seen = Set<String>()
    var queue: [URL] = []
    var busy = false
    var child: Process?
    var outputBuffer = Data()
    var result: [String: Any]?
    var failure: String?
    var cancelled = false
    var background = false
    var logHandle: FileHandle?
    var progressWindow: NSWindow?
    var progressLabel: NSTextField?
    var stopButton: NSButton?
    var retryTimer: Timer?
    var retryDelay: TimeInterval = 60
    var activity: NSObjectProtocol?
    var forceHashForQueue = false
    var quitting = false
    var paused = UserDefaults.standard.bool(forKey: "pausePrompts")

    func applicationDidFinishLaunching(_ notification: Notification) {
        // LaunchServices handles repeat opens; guard direct duplicate executions too.
        if NSRunningApplication.runningApplications(withBundleIdentifier: "io.github.andz200zx.immich-card-sync").contains(where: {$0.processIdentifier != ProcessInfo.processInfo.processIdentifier}) {
            NSApp.terminate(nil)
            return
        }
        try? fm.createDirectory(at: stateFolder, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.image = NSImage(systemSymbolName: "sdcard", accessibilityDescription: appName)
        statusItem.button?.toolTip = "Immich Card Sync — connect a camera card"
        let menu = NSMenu()
        statusLine = NSMenuItem(title: "Ready for camera cards", action: nil, keyEquivalent: "")
        menu.addItem(statusLine)
        menu.addItem(.separator())
        add(menu, "Sync connected cards…", #selector(syncCards))
        add(menu, "Verify card contents and sync…", #selector(verifyCards))
        add(menu, "Finish pending tags and stacks", #selector(finishPending))
        add(menu, "Show sync progress", #selector(showProgress))
        add(menu, "Open Immich", #selector(openImmich))
        add(menu, "View latest result", #selector(viewResult))
        add(menu, "Open logs", #selector(openLogs))
        pauseItem = add(menu, "Pause card prompts", #selector(togglePause))
        pauseItem.state = paused ? .on : .off
        menu.addItem(.separator())
        add(menu, "Quit Immich Card Sync", #selector(quit))
        statusItem.menu = menu
        let center = NSWorkspace.shared.notificationCenter
        center.addObserver(self, selector: #selector(mounted(_:)), name: NSWorkspace.didMountNotification, object: nil)
        center.addObserver(self, selector: #selector(unmounted(_:)), name: NSWorkspace.didUnmountNotification, object: nil)
        if configuration().isEmpty {
            message("Welcome to Immich Card Sync", "Run Install Immich Card Sync.command from the downloaded package to connect your server and enable card prompts.")
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) { self.discover() }
        schedulePending()
    }
    @discardableResult func add(_ menu: NSMenu, _ title: String, _ action: Selector) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: "")
        item.target = self
        menu.addItem(item)
        return item
    }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if busy { showProgress() } else { syncCards() }
        return true
    }
    @objc func mounted(_ notification: Notification) {
        guard let url = notification.userInfo?[NSWorkspace.volumeURLUserInfoKey] as? URL else { return }
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) { self.consider(url) }
    }
    @objc func unmounted(_ notification: Notification) {
        guard let url = notification.userInfo?[NSWorkspace.volumeURLUserInfoKey] as? URL else { return }
        seen = Set(seen.filter { !$0.hasPrefix(url.path + "|") })
        queue.removeAll { $0.path == url.path }
    }
    func discover() { for url in listCards() { consider(url) } }
    func consider(_ url: URL) {
        guard !paused, cameraCard(url) else { return }
        let identity = url.path + "|" + volumeIdentity(url)
        guard !seen.contains(identity) else { return }
        seen.insert(identity)
        queue.append(url)
        nextCard()
    }
    func nextCard() {
        guard !busy, !queue.isEmpty else { return }
        let url = queue.removeFirst()
        guard cameraCard(url) else { nextCard(); return }
        busy = true
        let alert = NSAlert()
        alert.messageText = "Sync with Immich?"
        alert.informativeText = "Camera card: \(url.lastPathComponent)\n\nUpload new photos and videos. RAW + JPEG pairs will be stacked with JPEG covers. Files stay on your card."
        alert.addButton(withTitle: "Sync")
        alert.addButton(withTitle: "Not now")
        NSApp.activate(ignoringOtherApps: true)
        let response = alert.runModal()
        busy = false
        if response == .alertFirstButtonReturn {
            start(root: url, forceHash: forceHashForQueue)
        } else { nextCard() }
    }
    @objc func syncCards() {
        if busy { showProgress(); return }
        let cards = listCards()
        if cards.isEmpty { message("No camera card connected", "Insert a DJI or Sony card, then try again."); return }
        queue = cards
        forceHashForQueue = false
        nextCard()
    }
    @objc func verifyCards() {
        if busy { showProgress(); return }
        queue = listCards()
        if queue.isEmpty { message("No camera card connected", "Insert a camera card, then try again."); return }
        forceHashForQueue = true
        nextCard()
    }
    func start(root: URL? = nil, forceHash: Bool = false, quiet: Bool = false) {
        guard !busy else { return }
        guard let python = configuration()["pythonPath"] as? String, fm.isExecutableFile(atPath: python) else {
            message("Setup needs attention", "Python or the Immich configuration is missing. Run Install Immich Card Sync.command again.")
            return
        }
        if let root = root, !cameraCard(root) { message("Card disconnected", "Reconnect the camera card and choose Sync connected cards."); return }
        busy = true
        background = quiet
        outputBuffer = Data()
        result = nil
        failure = nil
        cancelled = false
        retryTimer?.invalidate()
        retryTimer = nil
        let process = Process()
        process.executableURL = URL(fileURLWithPath: python)
        activity = ProcessInfo.processInfo.beginActivity(options: [.idleSystemSleepDisabled, .userInitiated], reason: "Syncing a camera card to Immich")
        var args = ["-u", Bundle.main.resourceURL!.appendingPathComponent("ingest.py").path, "--config", configURL.path, "--state", stateFolder.path]
        if let root = root { args += ["--root", root.path, "--volume-id", volumeIdentity(root)] }
        else { args.append("--finish-stacks") }
        if forceHash { args.append("--force-hash") }
        process.arguments = args
        process.currentDirectoryURL = support
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        let logs = support.appendingPathComponent("logs")
        try? fm.createDirectory(at: logs, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        let name = ISO8601DateFormatter().string(from: Date()).replacingOccurrences(of: ":", with: "-")
        let log = logs.appendingPathComponent("sync-\(name).jsonl")
        fm.createFile(atPath: log.path, contents: nil, attributes: [.posixPermissions: 0o600])
        logHandle = try? FileHandle(forWritingTo: log)
        trimLogs(logs)
        child = process
        statusLine.title = root == nil ? "Finishing RAW/JPEG stacks…" : "Syncing \(root!.lastPathComponent)…"
        statusItem.button?.title = " ↑"
        if !quiet { makeProgress(root?.lastPathComponent ?? "RAW/JPEG stacks") }
        do {
            try process.run()
            DispatchQueue.global(qos: .utility).async { [weak self] in
                while true {
                    let data = pipe.fileHandleForReading.availableData
                    if data.isEmpty { break }
                    DispatchQueue.main.async { self?.consume(data) }
                }
                process.waitUntilExit()
                DispatchQueue.main.async { self?.finished(process.terminationStatus) }
            }
        } catch { failure = "Could not start the importer: \(error.localizedDescription)"; finished(1) }
    }

    func consume(_ data: Data) {
        try? logHandle?.write(contentsOf: data)
        outputBuffer.append(data)
        while let end = outputBuffer.firstIndex(of: 10) {
            let line = outputBuffer[..<end]
            outputBuffer.removeSubrange(...end)
            guard let obj = try? JSONSerialization.jsonObject(with: line) as? [String: Any], let event = obj["event"] as? String else { continue }
            if event == "progress", let message = obj["message"] as? String { progressLabel?.stringValue = message }
            else if event == "result" { result = obj }
            else if event == "error" || event == "cancelled" { failure = obj["message"] as? String; cancelled = event == "cancelled" }
        }
    }
    func finished(_ code: Int32) {
        try? logHandle?.close()
        logHandle = nil
        child = nil
        if let activity = activity { ProcessInfo.processInfo.endActivity(activity); self.activity = nil }
        statusItem.button?.title = ""
        stopButton?.isEnabled = false
        if quitting { NSApp.terminate(nil); return }
        if code == 0, let result = result {
            let files = result["files"] as? Int ?? 0
            let uploaded = result["uploaded"] as? Int ?? 0
            let skipped = result["skipped"] as? Int ?? 0
            let pairs = result["pairs"] as? Int ?? 0
            let pending = result["pendingPairs"] as? Int ?? 0
            let warnings = result["warnings"] as? [String] ?? []
            var summary = result["stackOnly"] as? Bool == true ? "\(pairs) RAW/JPEG stacks checked with JPEG covers.\n\(result["tagged"] as? Int ?? 0) upload-date tags confirmed." : "\(uploaded) new files uploaded.\n\(skipped) already in Immich.\n\(pairs) RAW/JPEG stacks checked with JPEG covers."
            if let tag = result["batchTag"] as? String { summary += "\n\nUpload tag: \(tag)" }
            if files == 0, let msg = result["message"] as? String { summary = msg }
            if pending > 0 { summary += "\n\n\(pending) pairs are waiting for capture metadata. They will finish automatically while this app is running; the card is no longer needed for stacking." }
            if !warnings.isEmpty { summary += "\n\n\(warnings.count) pairing issues need review. See View latest result in the menu." }
            progressLabel?.stringValue = summary
            statusLine.title = pending > 0 ? "\(pending) RAW/JPEG pairs pending" : (warnings.isEmpty ? "Last sync completed" : "Sync completed — review pairing issues")
            if !background {
                message(warnings.isEmpty ? "Sync complete" : "Files synced — review pairing issues", summary + "\n\nYou can eject your card in Finder.")
                progressWindow?.orderOut(nil)
            }
            if pending > 0 {
                if background { retryDelay = min(retryDelay * 2, 900) }
                schedulePending()
            } else { retryDelay = 60 }
        } else {
            let text = failure ?? "Sync did not complete. Keep the card and run Sync connected cards to retry."
            progressLabel?.stringValue = text
            statusLine.title = cancelled ? "Sync stopped — safe to retry" : "Sync needs a retry"
            if !background { message(cancelled ? "Sync stopped" : "Sync did not complete", text) }
            else { retryDelay = min(retryDelay * 2, 900) }
            schedulePending()
        }
        busy = false
        nextCard()
    }
    func makeProgress(_ card: String) {
        if progressWindow == nil {
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 510, height: 210), styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
            window.title = appName
            window.isReleasedWhenClosed = false
            let heading = NSTextField(labelWithString: "Syncing camera card")
            heading.font = .boldSystemFont(ofSize: 19)
            heading.frame = NSRect(x: 24, y: 160, width: 460, height: 26)
            let label = NSTextField(wrappingLabelWithString: "Connecting to Immich…")
            label.frame = NSRect(x: 24, y: 56, width: 460, height: 96)
            label.maximumNumberOfLines = 5
            let button = NSButton(title: "Stop sync", target: self, action: #selector(stop))
            button.bezelStyle = .rounded
            button.frame = NSRect(x: 370, y: 16, width: 115, height: 30)
            window.contentView?.addSubview(heading)
            window.contentView?.addSubview(label)
            window.contentView?.addSubview(button)
            progressLabel = label
            stopButton = button
            progressWindow = window
            window.center()
        }
        progressWindow?.title = appName + " — " + card
        progressLabel?.stringValue = "Connecting to Immich…"
        stopButton?.isEnabled = true
        showProgress()
    }
    @objc func showProgress() {
        if let window = progressWindow { NSApp.activate(ignoringOtherApps: true); window.makeKeyAndOrderFront(nil) }
    }
    @objc func stop() {
        cancelled = true
        progressLabel?.stringValue = "Stopping safely…"
        stopButton?.isEnabled = false
        child?.terminate()
    }

    func message(_ title: String, _ body: String) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = body
        alert.addButton(withTitle: "OK")
        NSApp.activate(ignoringOtherApps: true)
        alert.runModal()
    }
    @objc func openImmich() {
        guard let base = (result?["server"] as? String) ?? (configuration()["servers"] as? [String])?.first else {
            message("Setup required", "Run Install Immich Card Sync.command first."); return
        }
        if let url = URL(string: base + "/photos") { NSWorkspace.shared.open(url) }
    }
    @objc func viewResult() {
        let path = stateFolder.appendingPathComponent("last-result.json")
        if fm.fileExists(atPath: path.path) { NSWorkspace.shared.open(path) }
        else { message("No completed sync yet", "Connect a camera card to start.") }
    }
    @objc func openLogs() { NSWorkspace.shared.open(support.appendingPathComponent("logs")) }
    @objc func togglePause() {
        paused.toggle()
        UserDefaults.standard.set(paused, forKey: "pausePrompts")
        pauseItem.state = paused ? .on : .off
        statusLine.title = paused ? "Card prompts paused" : "Ready for camera cards"
        if !paused { discover() }
    }
    @objc func finishPending() { if !busy { start() } }
    func schedulePending() {
        let hasPending = ["pending-stacks.json", "pending-tags.json"].contains { name in
            guard let data = try? Data(contentsOf: stateFolder.appendingPathComponent(name)),
                  let pending = try? JSONSerialization.jsonObject(with: data) as? [Any] else { return false }
            return !pending.isEmpty
        }
        guard retryTimer == nil, hasPending else { return }
        retryTimer = Timer.scheduledTimer(withTimeInterval: retryDelay, repeats: false) { [weak self] _ in
            guard let self = self else { return }
            self.retryTimer = nil
            if self.busy { self.schedulePending() } else { self.start(quiet: true) }
        }
    }
    func trimLogs(_ directory: URL) {
        let logs = ((try? fm.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil)) ?? []).filter { $0.pathExtension == "jsonl" }.sorted { $0.lastPathComponent > $1.lastPathComponent }
        for log in logs.dropFirst(30) { try? fm.removeItem(at: log) }
    }
    @objc func quit() {
        if busy { quitting = true; stop() }
        else { NSApp.terminate(nil) }
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let delegate = CardSync()
app.delegate = delegate
app.run()
