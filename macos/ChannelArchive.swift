import AppKit
import WebKit

final class ChannelArchiveApp: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate, WKDownloadDelegate {
    var window: NSWindow!
    var webView: WKWebView!
    var status: NSTextField!
    var spinner: NSProgressIndicator!
    var backend: Process?
    var ownsServer = false
    var ready = false
    var quitting = false
    var smokeStarted = false
    var outputBuffer = ""
    var errorOutput = ""
    var localURL: URL?
    var root: URL!
    var downloads: [ObjectIdentifier: (temporary: URL, destination: URL)] = [:]
    let smokeReport = ProcessInfo.processInfo.environment["CHANNEL_ARCHIVE_SMOKE_REPORT"]
    var smokeValue: [String: Any]?

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1160, height: 820),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "Channel Archive"
        window.minSize = NSSize(width: 740, height: 540)
        window.isReleasedWhenClosed = false
        window.delegate = self
        window.center()
        if smokeReport == nil { window.setFrameAutosaveName("ChannelArchiveMainWindow") }
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.translatesAutoresizingMaskIntoConstraints = false
        webView.isHidden = true
        status = NSTextField(wrappingLabelWithString: "Opening your archive…")
        status.font = .systemFont(ofSize: 15)
        status.alignment = .center
        status.translatesAutoresizingMaskIntoConstraints = false
        spinner = NSProgressIndicator()
        spinner.style = .spinning
        spinner.translatesAutoresizingMaskIntoConstraints = false
        spinner.startAnimation(nil)
        let content = window.contentView!
        [webView!, status!, spinner!].forEach { content.addSubview($0) }
        NSLayoutConstraint.activate([
            webView.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            webView.topAnchor.constraint(equalTo: content.topAnchor),
            webView.bottomAnchor.constraint(equalTo: content.bottomAnchor),
            status.centerXAnchor.constraint(equalTo: content.centerXAnchor),
            status.centerYAnchor.constraint(equalTo: content.centerYAnchor),
            status.widthAnchor.constraint(lessThanOrEqualToConstant: 560),
            spinner.centerXAnchor.constraint(equalTo: content.centerXAnchor),
            spinner.bottomAnchor.constraint(equalTo: status.topAnchor, constant: -18)
        ])
        window.makeKeyAndOrderFront(nil)
        if smokeReport == nil { NSApp.activate(ignoringOtherApps: true) }
        startBackend()
    }

    func buildMenu() {
        let menu = NSMenu()
        let appMenu = NSMenu()
        let appItem = NSMenuItem()
        appItem.submenu = appMenu
        appMenu.addItem(withTitle: "About Channel Archive", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Settings…", action: #selector(openSettings), keyEquivalent: ",").target = self
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Quit Channel Archive", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        menu.addItem(appItem)
        let fileMenu = NSMenu(title: "File")
        fileMenu.addItem(withTitle: "Show Archive Folder", action: #selector(showArchive), keyEquivalent: "").target = self
        fileMenu.addItem(withTitle: "Reload Window", action: #selector(reload), keyEquivalent: "r").target = self
        let fileItem = NSMenuItem(title: "File", action: nil, keyEquivalent: "")
        fileItem.submenu = fileMenu
        menu.addItem(fileItem)
        let editMenu = NSMenu(title: "Edit")
        for (title, action, key) in [("Undo", "undo:", "z"), ("Cut", "cut:", "x"), ("Copy", "copy:", "c"), ("Paste", "paste:", "v"), ("Select All", "selectAll:", "a")] {
            editMenu.addItem(withTitle: title, action: Selector(action), keyEquivalent: key)
        }
        let editItem = NSMenuItem(title: "Edit", action: nil, keyEquivalent: "")
        editItem.submenu = editMenu
        menu.addItem(editItem)
        NSApp.mainMenu = menu
    }

    func startBackend() {
        guard let resources = Bundle.main.resourceURL,
              let data = try? Data(contentsOf: resources.appendingPathComponent("launcher.json")),
              let config = (try? JSONSerialization.jsonObject(with: data)) as? [String: String],
              let python = config["python"], let library = config["library"] else {
            fail("The app is missing its launch settings. Rebuild it with tools/build_macos_app.py.")
            return
        }
        let override = ProcessInfo.processInfo.environment["CHANNEL_ARCHIVE_DATA_DIR"]
        if let override = override {
            root = URL(fileURLWithPath: override, isDirectory: true).standardizedFileURL
        } else if let encoded = config["libraryBookmark"] {
            var stale = false
            guard let bookmark = Data(base64Encoded: encoded),
                  let resolved = try? URL(resolvingBookmarkData: bookmark, options: .withoutUI, relativeTo: nil, bookmarkDataIsStale: &stale),
                  isDirectory(resolved) else {
                fail("Your archive folder is unavailable. Reconnect its drive or restore the folder, then reopen Channel Archive. Your saved library has not been replaced.")
                return
            }
            root = resolved.standardizedFileURL
        } else {
            root = URL(fileURLWithPath: library, isDirectory: true).standardizedFileURL
            guard isDirectory(root) else {
                fail("Your archive folder has moved or is unavailable. Rebuild Channel Archive from the folder’s new location to reconnect your saved library.")
                return
            }
        }
        guard FileManager.default.isExecutableFile(atPath: python) else {
            fail("The Python installation used to build this app is no longer available. Install Python 3.10 or newer, then rebuild the app.")
            return
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: python)
        process.currentDirectoryURL = resources.appendingPathComponent("app")
        process.arguments = ["-u", "-m", "telegram_scraper", "--data-dir", root.path, "serve", "--port", "0", "--no-browser", "--desktop"]
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONPATH"] = resources.appendingPathComponent("app").path
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        process.environment = environment
        let output = Pipe()
        let errors = Pipe()
        process.standardOutput = output
        process.standardError = errors
        output.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty { handle.readabilityHandler = nil; return }
            DispatchQueue.main.async { self?.readOutput(String(decoding: data, as: UTF8.self)) }
        }
        errors.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty { handle.readabilityHandler = nil; return }
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.errorOutput = String((self.errorOutput + String(decoding: data, as: UTF8.self)).suffix(6000))
            }
        }
        process.terminationHandler = { [weak self] process in
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) {
                guard let self = self else { return }
                if self.quitting { NSApp.reply(toApplicationShouldTerminate: true); return }
                if self.ready && !self.ownsServer && process.terminationStatus == 0 { return }
                self.fail(self.errorOutput.isEmpty ? "The local archive server stopped. Reopen Channel Archive to try again. Your saved files are kept." : self.errorOutput)
            }
        }
        backend = process
        do { try process.run() }
        catch { fail("The archive could not start. \(error.localizedDescription)") }
    }

    func readOutput(_ text: String) {
        outputBuffer += text
        while let newline = outputBuffer.firstIndex(of: "\n") {
            let line = String(outputBuffer[..<newline])
            outputBuffer.removeSubrange(...newline)
            guard let data = line.data(using: .utf8),
                  let value = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
                  value["event"] as? String == "ready",
                  let address = value["url"] as? String,
                  let url = URL(string: address), url.scheme == "http", url.host == "127.0.0.1",
                  url.port != nil, url.user == nil, url.password == nil else { continue }
            ready = true
            ownsServer = value["owns_server"] as? Bool == true
            localURL = url
            webView.load(URLRequest(url: url))
        }
    }

    func fail(_ message: String) {
        spinner.stopAnimation(nil)
        spinner.isHidden = true
        webView.isHidden = true
        status.isHidden = false
        status.stringValue = message
        if let report = smokeReport {
            writeReport(["ok": false, "error": message], to: report)
            NSApp.terminate(nil)
        }
    }

    @objc func openSettings() { webView.evaluateJavaScript("document.getElementById('settings-open')?.click()") }
    @objc func reload() { if let url = localURL { webView.load(URLRequest(url: url)) } }
    @objc func showArchive() { if let root = root { NSWorkspace.shared.open(root) } }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        window.makeKeyAndOrderFront(nil)
        return true
    }

    func application(_ application: NSApplication, open urls: [URL]) {
        // The launch link only brings up this app. It cannot choose a library,
        // supply credentials, or trigger scraping from an external page.
        guard urls.contains(where: { $0.scheme == "channel-archive" && $0.host == "open" &&
            ["", "/"].contains($0.path) && $0.query == nil && $0.fragment == nil &&
            $0.user == nil && $0.password == nil && $0.port == nil }) else { return }
        window?.makeKeyAndOrderFront(nil)
        application.activate(ignoringOtherApps: true)
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool { NSApp.terminate(nil); return false }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if quitting { return .terminateLater }
        if let process = backend, process.isRunning && (ownsServer || !ready) {
            quitting = true
            status.stringValue = "Stopping safely. Finishing saved work before closing…"
            status.isHidden = false
            webView.isHidden = true
            spinner.isHidden = false
            spinner.startAnimation(nil)
            process.interrupt()
            return .terminateLater
        }
        return .terminateNow
    }

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url else { decisionHandler(.cancel); return }
        let local = url.scheme == localURL?.scheme && url.host == localURL?.host && url.port == localURL?.port
        let localBlob = action.shouldPerformDownload && localURL.map { url.absoluteString.hasPrefix("blob:" + $0.absoluteString + "/") } == true
        if local || localBlob {
            decisionHandler(action.shouldPerformDownload ? .download : .allow)
        } else {
            decisionHandler(.cancel)
            if action.navigationType == .linkActivated && ["https", "http"].contains(url.scheme ?? "") { NSWorkspace.shared.open(url) }
        }
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration, for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url, ["https", "http"].contains(url.scheme ?? "") { NSWorkspace.shared.open(url) }
        return nil
    }

    func webView(_ webView: WKWebView, decidePolicyFor response: WKNavigationResponse, decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        let attachment = (response.response as? HTTPURLResponse)?.value(forHTTPHeaderField: "Content-Disposition")?.lowercased().hasPrefix("attachment") == true
        decisionHandler(attachment || !response.canShowMIMEType ? .download : .allow)
    }

    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) { download.delegate = self }
    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) { download.delegate = self }

    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse, suggestedFilename: String, completionHandler: @escaping (URL?) -> Void) {
        if smokeReport != nil, let path = ProcessInfo.processInfo.environment["CHANNEL_ARCHIVE_SMOKE_EXPORT"] {
            let destination = URL(fileURLWithPath: path)
            let temporary = destination.deletingLastPathComponent().appendingPathComponent(".channel-archive-\(UUID().uuidString).partial")
            downloads[ObjectIdentifier(download)] = (temporary, destination)
            completionHandler(temporary)
            return
        }
        let panel = NSSavePanel()
        panel.nameFieldStringValue = URL(fileURLWithPath: suggestedFilename).lastPathComponent
        panel.canCreateDirectories = true
        panel.beginSheetModal(for: window) { choice in
            guard choice == .OK, let destination = panel.url else { completionHandler(nil); return }
            let temporary = destination.deletingLastPathComponent().appendingPathComponent(".channel-archive-\(UUID().uuidString).partial")
            self.downloads[ObjectIdentifier(download)] = (temporary, destination)
            completionHandler(temporary)
        }
    }

    func downloadDidFinish(_ download: WKDownload) {
        guard let files = downloads.removeValue(forKey: ObjectIdentifier(download)) else { return }
        do {
            if FileManager.default.fileExists(atPath: files.destination.path) {
                // Save-panel replacement is explicit. Retain the previous file
                // while atomically publishing the completely downloaded copy.
                let backup = files.destination.lastPathComponent + ".previous-" + UUID().uuidString
                _ = try FileManager.default.replaceItemAt(files.destination, withItemAt: files.temporary, backupItemName: backup, options: .withoutDeletingBackupItem)
            } else {
                try FileManager.default.moveItem(at: files.temporary, to: files.destination)
            }
            if let report = smokeReport, var value = smokeValue {
                value["export_completed"] = true
                writeReport(value, to: report)
                NSApp.terminate(nil)
            }
        } catch {
            if smokeReport != nil { fail("Native export could not publish: \(error.localizedDescription)"); return }
            let alert = NSAlert()
            alert.messageText = "The download could not be saved"
            alert.informativeText = "Your existing file is kept. The completed download is at \(files.temporary.path)."
            alert.beginSheetModal(for: window)
        }
    }

    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        if let files = downloads.removeValue(forKey: ObjectIdentifier(download)) { try? FileManager.default.removeItem(at: files.temporary) }
        if (error as NSError).code == NSURLErrorCancelled { return }
        if smokeReport != nil { fail("Native export failed: \(error.localizedDescription)"); return }
        let alert = NSAlert()
        alert.messageText = "The download did not finish"
        alert.informativeText = "Your archive and any existing destination file are kept. Try the download again."
        alert.beginSheetModal(for: window)
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        webView.isHidden = false
        spinner.stopAnimation(nil)
        spinner.isHidden = true
        status.isHidden = true
        guard let report = smokeReport, !smokeStarted else { return }
        smokeStarted = true
        let script = """
        for (let n=0;n<100 && document.documentElement.dataset.appReady !== 'true';n++) await new Promise(r=>setTimeout(r,100));
        const state = await (await fetch('/api/state')).json();
        return {ok:document.documentElement.dataset.appReady==='true',title:document.title,setupVisible:!document.getElementById('setup-panel').hidden,posts:state.library.total,authorized:state.connection.authorized,setupAction:document.getElementById('setup-action').textContent};
        """
        webView.callAsyncJavaScript(script, arguments: [:], in: nil, in: .page) { result in
            switch result {
            case .success(let value):
                var value = value as? [String: Any] ?? ["ok": false]
                value["owns_server"] = self.ownsServer
                self.writeReport(value, to: report)
                webView.takeSnapshot(with: nil) { image, _ in
                    if let tiff = image?.tiffRepresentation, let bitmap = NSBitmapImageRep(data: tiff), let png = bitmap.representation(using: .png, properties: [:]) {
                        try? png.write(to: URL(fileURLWithPath: report + ".png"))
                    }
                    if ProcessInfo.processInfo.environment["CHANNEL_ARCHIVE_SMOKE_EXPORT"] != nil {
                        self.smokeValue = value
                        webView.evaluateJavaScript("document.getElementById('export-button').click()")
                        DispatchQueue.main.asyncAfter(deadline: .now() + 20) {
                            if !self.quitting { self.fail("The native export did not finish.") }
                        }
                    } else { NSApp.terminate(nil) }
                }
            case .failure(let error): self.fail(error.localizedDescription)
            }
        }
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        if (error as NSError).code != NSURLErrorCancelled { fail("The archive window could not load. Use File → Reload Window to try again.") }
    }

    func writeReport(_ value: [String: Any], to path: String) {
        if let data = try? JSONSerialization.data(withJSONObject: value, options: [.prettyPrinted, .sortedKeys]) {
            try? data.write(to: URL(fileURLWithPath: path), options: .atomic)
        }
    }
}

func isDirectory(_ url: URL) -> Bool {
    var directory = ObjCBool(false)
    return FileManager.default.fileExists(atPath: url.path, isDirectory: &directory) && directory.boolValue
}

func renderIcon(to path: String) {
    let image = NSImage(size: NSSize(width: 1024, height: 1024))
    image.lockFocus()
    NSColor(calibratedRed: 0.294, green: 0.333, blue: 0.812, alpha: 1).setFill()
    NSBezierPath(roundedRect: NSRect(x: 80, y: 80, width: 864, height: 864), xRadius: 190, yRadius: 190).fill()
    NSColor.white.setStroke()
    let lid = NSBezierPath(roundedRect: NSRect(x: 265, y: 574, width: 494, height: 134), xRadius: 14, yRadius: 14)
    lid.lineWidth = 35; lid.stroke()
    let box = NSBezierPath(roundedRect: NSRect(x: 302, y: 307, width: 420, height: 267), xRadius: 16, yRadius: 16)
    box.lineWidth = 35; box.stroke()
    let handle = NSBezierPath()
    handle.move(to: NSPoint(x: 450, y: 475)); handle.line(to: NSPoint(x: 574, y: 475))
    handle.lineWidth = 35; handle.lineCapStyle = .round; handle.stroke()
    image.unlockFocus()
    if let tiff = image.tiffRepresentation, let bitmap = NSBitmapImageRep(data: tiff), let png = bitmap.representation(using: .png, properties: [:]) {
        try? png.write(to: URL(fileURLWithPath: path))
    }
}

let app = NSApplication.shared
if CommandLine.arguments.count == 3 && CommandLine.arguments[1] == "--render-icon" {
    renderIcon(to: CommandLine.arguments[2])
} else if CommandLine.arguments.count == 3 && CommandLine.arguments[1] == "--bookmark-library" {
    do {
        let url = URL(fileURLWithPath: CommandLine.arguments[2], isDirectory: true)
        let bookmark = try url.bookmarkData(options: [], includingResourceValuesForKeys: nil, relativeTo: nil)
        print(bookmark.base64EncodedString())
    } catch {
        fputs("Could not remember the archive folder: \(error.localizedDescription)\n", stderr)
        exit(1)
    }
} else if CommandLine.arguments.count == 4 && CommandLine.arguments[1] == "--make-alias" {
    do {
        let target = URL(fileURLWithPath: CommandLine.arguments[2], isDirectory: true)
        let destination = URL(fileURLWithPath: CommandLine.arguments[3])
        let bookmark = try target.bookmarkData(options: .suitableForBookmarkFile, includingResourceValuesForKeys: nil, relativeTo: nil)
        try URL.writeBookmarkData(bookmark, to: destination)
    } catch {
        fputs("Could not create the app shortcut: \(error.localizedDescription)\n", stderr)
        exit(1)
    }
} else {
    let delegate = ChannelArchiveApp()
    app.delegate = delegate
    app.setActivationPolicy(.regular)
    app.run()
}
