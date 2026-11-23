// Claude Switcher: a menu-bar account switcher for the Claude app (claude.ai desktop).
//
// How it works: the Claude login is stored in several files in ~/Library/Application Support/Claude
// (cookies, Local Storage, Session Storage, IndexedDB) and in several config.json keys
// (the OAuth token, which the Code tab also uses). For each account we keep a copy of these
// files. Switching: quit Claude -> save the current account -> put the target account's files
// in place -> launch Claude. Everything else (Code sessions, settings, caches, VM) is shared.

import AppKit
import ServiceManagement

// MARK: - Paths and constants

let claudeBundleID = "com.anthropic.claudefordesktop"
let fm = FileManager.default
let home = fm.homeDirectoryForCurrentUser

let claudeDir: URL = {
    if let p = ProcessInfo.processInfo.environment["CLAUDE_SWITCHER_CLAUDE_DIR"] { return URL(fileURLWithPath: p) }
    return home.appendingPathComponent("Library/Application Support/Claude")
}()
let storeDir: URL = {
    if let p = ProcessInfo.processInfo.environment["CLAUDE_SWITCHER_STORE_DIR"] { return URL(fileURLWithPath: p) }
    return home.appendingPathComponent("Library/Application Support/ClaudeSwitcher")
}()
let profilesDir = storeDir.appendingPathComponent("profiles")
let backupsDir = storeDir.appendingPathComponent("backups")
let accountsFile = storeDir.appendingPathComponent("accounts.json")
let configFile = claudeDir.appendingPathComponent("config.json")

/// Files and folders that hold the account login.
let authItems = ["Cookies", "Cookies-journal", "Local Storage", "Session Storage", "IndexedDB"]
/// config.json keys that belong to the account.
let authConfigKeys = ["lastKnownAccountUuid", "bootSideSessions", "sshTransportFlagLastAnswer"]
let authConfigPrefixes = ["oauth:"]

func isAuthConfigKey(_ k: String) -> Bool {
    authConfigKeys.contains(k) || authConfigPrefixes.contains { k.hasPrefix($0) }
}

// MARK: - Interface language
// The interface is English; Russian is used only when Russian is the first preferred system language.
// The log, the command line and the self-test are always English (the log is parsed by usage/core.py).

let interfaceIsRussian = Locale.preferredLanguages.first?.lowercased().hasPrefix("ru") ?? false

/// Interface text: the English string, or the Russian one on a Russian system.
func L(_ en: String, _ ru: String) -> String { interfaceIsRussian ? ru : en }

// MARK: - Log
// ~/Library/Logs/ClaudeSwitcher/switcher.log: every switcher action and the sign-in/sign-out events from the Claude logs.

enum Log {
    static let dir: URL = {
        if let p = ProcessInfo.processInfo.environment["CLAUDE_SWITCHER_LOG_DIR"] { return URL(fileURLWithPath: p) }
        return home.appendingPathComponent("Library/Logs/ClaudeSwitcher")
    }()
    static let file = dir.appendingPathComponent("switcher.log")
    static let queue = DispatchQueue(label: "switcher.log")
    static let maxSize: UInt64 = 2_000_000

    static func write(_ msg: String) {
        let f = DateFormatter(); f.dateFormat = "yyyy-MM-dd HH:mm:ss"
        let line = "\(f.string(from: Date())) \(msg)\n"
        FileHandle.standardError.write(line.data(using: .utf8)!)
        queue.async {
            try? fm.createDirectory(at: dir, withIntermediateDirectories: true)
            if let size = (try? fm.attributesOfItem(atPath: file.path))?[.size] as? UInt64, size > maxSize {
                let old = dir.appendingPathComponent("switcher.1.log")
                try? fm.removeItem(at: old); try? fm.moveItem(at: file, to: old)
            }
            if !fm.fileExists(atPath: file.path) { fm.createFile(atPath: file.path, contents: nil) }
            if let h = try? FileHandle(forWritingTo: file) {
                h.seekToEndOfFile(); h.write(line.data(using: .utf8)!); try? h.close()
            }
        }
    }

    /// Short description of the login files in a folder: sizes and account UUID (without the tokens themselves).
    static func describe(_ dir: URL, configName: String) -> String {
        var parts: [String] = []
        for item in authItems {
            let u = dir.appendingPathComponent(item)
            guard fm.fileExists(atPath: u.path) else { parts.append("\(item)=none"); continue }
            parts.append("\(item)=\(sizeOf(u))")
        }
        if let data = try? Data(contentsOf: dir.appendingPathComponent(configName)),
           let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            parts.append("uuid=\(obj["lastKnownAccountUuid"] as? String ?? "-")")
            parts.append("oauth=\(obj.keys.filter { $0.hasPrefix("oauth:") }.sorted().joined(separator: ","))")
        }
        return parts.joined(separator: " ")
    }

    static func sizeOf(_ u: URL) -> Int {
        var isDir: ObjCBool = false
        guard fm.fileExists(atPath: u.path, isDirectory: &isDir) else { return 0 }
        if !isDir.boolValue { return (try? fm.attributesOfItem(atPath: u.path))?[.size] as? Int ?? 0 }
        var total = 0
        for case let f as URL in fm.enumerator(at: u, includingPropertiesForKeys: [.fileSizeKey]) ?? .init() {
            total += (try? f.resourceValues(forKeys: [.fileSizeKey]))?.fileSize ?? 0
        }
        return total
    }
}

// MARK: - Watching Claude sign-ins through its log
// Claude writes lines like these to ~/Library/Logs/Claude/main.log
//   [account] Login-state transition (loggedOut: false → true, uuid: <A> → <none>)
// They show when an account signed in to or out of Claude.

enum ClaudeAuthEvent {
    case loggedOut(from: String?)
    case loggedIn(from: String?, to: String)
    case forbidden(String)          // "organization read returned 403" and the like: the server did not accept the saved login
}

@MainActor
final class ClaudeLogWatcher {
    let url: URL = {
        if let p = ProcessInfo.processInfo.environment["CLAUDE_SWITCHER_CLAUDE_LOG"] { return URL(fileURLWithPath: p) }
        return home.appendingPathComponent("Library/Logs/Claude/main.log")
    }()
    var offset: UInt64 = 0
    var fileId: Int = 0
    var pending = ""
    var timer: Timer?
    let onEvent: (ClaudeAuthEvent) -> Void

    init(onEvent: @escaping (ClaudeAuthEvent) -> Void) { self.onEvent = onEvent }

    func start() {
        let (id, size) = stat()
        fileId = id; offset = size          // old history is not parsed
        timer = Timer.scheduledTimer(withTimeInterval: 1.5, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.poll() }
        }
        Log.write("watcher: watching \(url.path) (from offset \(size))")
    }

    func stat() -> (Int, UInt64) {
        guard let a = try? fm.attributesOfItem(atPath: url.path) else { return (0, 0) }
        return ((a[.systemFileNumber] as? Int) ?? 0, (a[.size] as? UInt64) ?? 0)
    }

    func poll() {
        let (id, size) = stat()
        if id != fileId || size < offset {   // the Claude log rotated (main.log -> main1.log)
            fileId = id; offset = 0; pending = ""
        }
        guard size > offset, let h = try? FileHandle(forReadingFrom: url) else { return }
        defer { try? h.close() }
        try? h.seek(toOffset: offset)
        let data = h.readData(ofLength: Int(min(size - offset, 4_000_000)))
        offset += UInt64(data.count)
        pending += String(decoding: data, as: UTF8.self)
        var lines = pending.components(separatedBy: "\n")
        pending = lines.removeLast()
        for l in lines { parse(l) }
    }

    static let transition = try! NSRegularExpression(
        pattern: #"Login-state transition \(loggedOut: (true|false) → (true|false), uuid: (\S+) → ([^\s,)]+)"#)

    func parse(_ line: String) {
        guard line.contains("[account]") else { return }
        let r = NSRange(line.startIndex..., in: line)
        if let m = Self.transition.firstMatch(in: line, range: r) {
            func g(_ i: Int) -> String { String(line[Range(m.range(at: i), in: line)!]) }
            let wasOut = g(1) == "true", nowOut = g(2) == "true"
            let from = g(3) == "<none>" ? nil : g(3), to = g(4) == "<none>" ? nil : g(4)
            Log.write("claude: \(line.drop { $0 != "[" })")
            if !wasOut && nowOut { onEvent(.loggedOut(from: from)) }
            else if wasOut && !nowOut, let to { onEvent(.loggedIn(from: from, to: to)) }
        } else if line.contains("Navigated to /logout") || line.contains("returned 403") {
            Log.write("claude: \(line.drop { $0 != "[" })")
            if line.contains("403") { onEvent(.forbidden(line)) }
        }
    }
}

// MARK: - Model

struct Account: Codable, Equatable {
    var id: String
    var name: String
    var accountUuid: String?
    var savedAt: Date?
    /// The saved login is no longer accepted by the server (the account was signed out in Claude itself).
    var needsRelogin: Bool? = nil
    /// Connect VPN Happ before switching to this account, and disconnect and quit Happ when leaving it.
    var useHapp: Bool? = nil
}

struct Store: Codable {
    var accounts: [Account] = []
    var activeId: String?
    var askOnClaudeLaunch: Bool = true
    var didInitialBackup: Bool = false
    var menuIcon: String = MenuIcon.defaultId

    init() {}
    init(from d: Decoder) throws {
        let c = try d.container(keyedBy: CodingKeys.self)
        accounts = try c.decodeIfPresent([Account].self, forKey: .accounts) ?? []
        activeId = try c.decodeIfPresent(String.self, forKey: .activeId)
        askOnClaudeLaunch = try c.decodeIfPresent(Bool.self, forKey: .askOnClaudeLaunch) ?? true
        didInitialBackup = try c.decodeIfPresent(Bool.self, forKey: .didInitialBackup) ?? false
        menuIcon = try c.decodeIfPresent(String.self, forKey: .menuIcon) ?? MenuIcon.defaultId
    }
}

// MARK: - Menu bar icon

struct MenuIcon {
    let id: String
    let title: String
    let showsLetter: Bool
    let make: () -> NSImage?

    static let defaultId = "duo"

    static let all: [MenuIcon] = [
        MenuIcon(id: "duo", title: L("Two silhouettes (like the app icon)", "Два силуэта (как иконка приложения)"), showsLetter: false) { duoImage(gap: true) },
        MenuIcon(id: "duo-letter", title: L("Two silhouettes + account letter", "Два силуэта + буква аккаунта"), showsLetter: true) { duoImage(gap: true) },
        MenuIcon(id: "duo-flat", title: L("Two silhouettes, same brightness", "Два силуэта, оба одной яркости"), showsLetter: false) { duoImage(gap: true, backAlpha: 1) },
        MenuIcon(id: "person.2.fill", title: L("System: two people", "Системный: два человечка"), showsLetter: false) { symbol("person.2.fill", 15) },
        MenuIcon(id: "person.2", title: L("System: two people, outline", "Системный: два человечка, контур"), showsLetter: false) { symbol("person.2", 15) },
        MenuIcon(id: "person.line.dotted.person.fill", title: L("System: two people, dotted line", "Системный: два человечка с пунктиром"), showsLetter: false) { symbol("person.line.dotted.person.fill", 14) },
        MenuIcon(id: "person.fill.and.arrow.left.and.arrow.right", title: L("System: person with arrows", "Системный: человечек со стрелками"), showsLetter: false) { symbol("person.fill.and.arrow.left.and.arrow.right", 14) },
        MenuIcon(id: "person.2.circle", title: L("Old: people in a circle", "Старый: человечки в кружке"), showsLetter: false) { symbol("person.2.circle", 13) },
    ]

    static func find(_ id: String) -> MenuIcon { all.first { $0.id == id } ?? all[0] }

    static func symbol(_ name: String, _ size: CGFloat) -> NSImage? {
        let cfg = NSImage.SymbolConfiguration(pointSize: size, weight: .medium)
        let img = NSImage(systemSymbolName: name, accessibilityDescription: L("Claude accounts", "Аккаунты Claude"))?.withSymbolConfiguration(cfg)
        img?.isTemplate = true
        return img
    }

    /// The same two silhouettes as on the app icon: the back one semi-transparent, a cutout around the front one.
    static func duoImage(gap: Bool, backAlpha: CGFloat = 0.5, size: CGFloat = 18) -> NSImage {
        let img = NSImage(size: NSSize(width: size, height: size), flipped: true) { _ in
            guard let ctx = NSGraphicsContext.current?.cgContext else { return false }
            // Geometry from AppIcon.svg (silhouette group 233…794 × 243…803), fitted into size×size.
            let s = (size - 0.6) / 561.0, ox = 0.3 - 233 * s, oy = 0.3 - 243 * s
            func P(_ x: CGFloat, _ y: CGFloat) -> CGPoint { CGPoint(x: ox + x * s, y: oy + y * s) }
            func body(_ x0: CGFloat, _ x1: CGFloat, _ yc: CGFloat, _ ry: CGFloat, _ yb: CGFloat, _ r: CGFloat) -> CGPath {
                let a = P(x0, yc), b = P(x1, yc), bottom = P(x0, yb).y, rad = r * s
                let rx = (b.x - a.x) / 2, k = (ry * s) / rx
                let path = CGMutablePath()
                path.move(to: CGPoint(x: a.x, y: bottom - rad))
                path.addLine(to: a)
                path.addArc(center: CGPoint(x: a.x + rx, y: a.y / k), radius: rx, startAngle: .pi, endAngle: 2 * .pi,
                            clockwise: false, transform: CGAffineTransform(scaleX: 1, y: k))
                path.addLine(to: CGPoint(x: b.x, y: bottom - rad))
                path.addArc(tangent1End: CGPoint(x: b.x, y: bottom), tangent2End: CGPoint(x: b.x - rad, y: bottom), radius: rad)
                path.addArc(tangent1End: CGPoint(x: a.x, y: bottom), tangent2End: CGPoint(x: a.x, y: bottom - rad), radius: rad)
                path.closeSubpath()
                return path
            }
            func head(_ x: CGFloat, _ y: CGFloat, _ r: CGFloat) -> CGPath {
                let c = P(x, y); return CGPath(ellipseIn: CGRect(x: c.x - r * s, y: c.y - r * s, width: 2 * r * s, height: 2 * r * s), transform: nil)
            }
            let backHead = head(408, 330, 87.4), backBody = body(233.2, 582.8, 581.16, 138, 682.36, 46)
            let frontHead = head(604, 420, 95), frontBody = body(414, 794, 693, 150, 803, 50)
            ctx.setFillColor(NSColor.black.cgColor)
            ctx.setAlpha(backAlpha)
            ctx.beginTransparencyLayer(auxiliaryInfo: nil)
            ctx.addPath(backHead); ctx.addPath(backBody); ctx.fillPath()
            if gap {   // cutout around the front silhouette so the two do not merge
                ctx.setBlendMode(.clear)
                ctx.setLineWidth(2.6); ctx.setLineJoin(.round)
                for p in [frontHead, frontBody] { ctx.addPath(p); ctx.drawPath(using: .fillStroke) }
                ctx.setBlendMode(.normal)
            }
            ctx.endTransparencyLayer()
            ctx.setAlpha(1)
            ctx.addPath(frontHead); ctx.addPath(frontBody); ctx.fillPath()
            return true
        }
        img.isTemplate = true
        img.accessibilityDescription = L("Claude accounts", "Аккаунты Claude")
        return img
    }
}

enum SwitchError: LocalizedError {
    case message(String)
    var errorDescription: String? { if case .message(let m) = self { return m }; return nil }
}

// MARK: - Profile storage

final class Profiles {
    var store = Store()

    init() { load() }

    func load() {
        guard let data = try? Data(contentsOf: accountsFile) else { return }
        let dec = JSONDecoder(); dec.dateDecodingStrategy = .iso8601
        if let s = try? dec.decode(Store.self, from: data) { store = s }
    }

    func save() {
        try? fm.createDirectory(at: storeDir, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        let enc = JSONEncoder(); enc.dateEncodingStrategy = .iso8601
        enc.outputFormatting = [.prettyPrinted, .sortedKeys]
        if let data = try? enc.encode(store) { try? data.write(to: accountsFile, options: .atomic) }
    }

    var active: Account? { store.accounts.first { $0.id == store.activeId } }

    func account(withUuid uuid: String) -> Account? { store.accounts.first { $0.accountUuid == uuid } }

    // --- config.json

    func readConfig() -> [String: Any] {
        guard let data = try? Data(contentsOf: configFile),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return [:] }
        return obj
    }

    func writeConfig(_ obj: [String: Any]) throws {
        let data = try JSONSerialization.data(withJSONObject: obj, options: [.prettyPrinted, .withoutEscapingSlashes])
        try data.write(to: configFile, options: .atomic)
    }

    /// UUID of the account currently signed in to Claude (nil: nobody is signed in).
    func currentUuid() -> String? {
        guard hasLoginFiles() else { return nil }
        return readConfig()["lastKnownAccountUuid"] as? String
    }

    func hasLoginFiles() -> Bool { fm.fileExists(atPath: claudeDir.appendingPathComponent("Cookies").path) }

    // --- snapshot and restore

    func profileDir(_ a: Account) -> URL { profilesDir.appendingPathComponent(a.id) }

    /// Copies the current login from the Claude folder into the account profile.
    func snapshot(into a: inout Account) throws {
        try fm.createDirectory(at: profilesDir, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        let tmp = profilesDir.appendingPathComponent(".tmp-\(a.id)-\(UUID().uuidString)")
        try fm.createDirectory(at: tmp, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        do {
            for item in authItems {
                let src = claudeDir.appendingPathComponent(item)
                if fm.fileExists(atPath: src.path) { try fm.copyItem(at: src, to: tmp.appendingPathComponent(item)) }
            }
            let cfg = readConfig().filter { isAuthConfigKey($0.key) }
            let data = try JSONSerialization.data(withJSONObject: cfg, options: [.prettyPrinted])
            try data.write(to: tmp.appendingPathComponent("config-auth.json"))
        } catch {
            try? fm.removeItem(at: tmp); throw error
        }
        let dest = profileDir(a)
        // Swap in one step: a crash between "remove" and "move" would leave the account without saved files.
        if fm.fileExists(atPath: dest.path) { _ = try fm.replaceItemAt(dest, withItemAt: tmp) }
        else { try fm.moveItem(at: tmp, to: dest) }
        a.accountUuid = readConfig()["lastKnownAccountUuid"] as? String ?? a.accountUuid
        a.savedAt = Date()
        a.needsRelogin = nil
        Log.write("snapshot “\(a.name)” (Claude \(Claude.running == nil ? "closed" : "open")): \(Log.describe(dest, configName: "config-auth.json"))")
    }

    /// Puts the login from a profile into the Claude folder (or clears the login if the profile is nil).
    func restore(_ a: Account?) throws {
        let src = a.map(profileDir)
        if let src, !fm.fileExists(atPath: src.path) {
            Log.write("restore “\(a!.name)”: no profile folder \(src.path)")
            throw SwitchError.message(L("No saved files for “\(a!.name)”. Sign in to this account and save it again.",
                                        "Нет сохранённых файлов для «\(a!.name)». Войдите в этот аккаунт и сохраните его заново."))
        }
        Log.write("restore \(a.map { "“\($0.name)”: " + Log.describe(profileDir($0), configName: "config-auth.json") } ?? "empty login (sign-in screen)")")
        for item in authItems {
            let dst = claudeDir.appendingPathComponent(item)
            if fm.fileExists(atPath: dst.path) || (try? dst.checkResourceIsReachable()) == true { try fm.removeItem(at: dst) }
            if let s = src?.appendingPathComponent(item), fm.fileExists(atPath: s.path) { try fm.copyItem(at: s, to: dst) }
        }
        var cfg = readConfig()
        for k in cfg.keys where isAuthConfigKey(k) { cfg.removeValue(forKey: k) }
        if let s = src?.appendingPathComponent("config-auth.json"),
           let data = try? Data(contentsOf: s),
           let saved = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            for (k, v) in saved { cfg[k] = v }
        }
        try writeConfig(cfg)
        // usage/core.py (switch_timeline) parses this line: do not reword "restore done" or the uuid= part.
        // Builds before November 2026 wrote it in Russian ("restore готово"); core.py accepts both forms.
        Log.write("restore done, in the Claude folder: \(Log.describe(claudeDir, configName: "config.json"))")
    }

    /// One-time backup of the original files before the first switch.
    func initialBackupIfNeeded() throws {
        guard !store.didInitialBackup else { return }
        let f = DateFormatter(); f.dateFormat = "yyyy-MM-dd_HH-mm-ss"
        let dir = backupsDir.appendingPathComponent("original-\(f.string(from: Date()))")
        try fm.createDirectory(at: dir, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        for item in authItems + ["config.json"] {
            let src = claudeDir.appendingPathComponent(item)
            if fm.fileExists(atPath: src.path) { try fm.copyItem(at: src, to: dir.appendingPathComponent(item)) }
        }
        store.didInitialBackup = true
        save()
        Log.write("backup: \(dir.path)")
    }

    /// Saves the current login into an existing profile (by UUID) or into a new one named name.
    @discardableResult
    func saveCurrent(name: String?) throws -> Account? {
        guard let uuid = currentUuid() else { return nil }
        if var a = account(withUuid: uuid) {
            try snapshot(into: &a)
            if let i = store.accounts.firstIndex(where: { $0.id == a.id }) { store.accounts[i] = a }
            store.activeId = a.id; save(); return a
        }
        guard let name, !name.isEmpty else { return nil }
        var a = Account(id: UUID().uuidString, name: name, accountUuid: uuid, savedAt: nil)
        try snapshot(into: &a)
        store.accounts.append(a); store.activeId = a.id; save()
        return a
    }

    func setNeedsRelogin(uuid: String, _ v: Bool) {
        guard let i = store.accounts.firstIndex(where: { $0.accountUuid == uuid }) else { return }
        store.accounts[i].needsRelogin = v ? true : nil; save()
    }

    func remove(_ a: Account) {
        Log.write("removed account “\(a.name)” \(a.accountUuid ?? "-")")
        try? fm.removeItem(at: profileDir(a))
        store.accounts.removeAll { $0.id == a.id }
        if store.activeId == a.id { store.activeId = nil }
        save()
    }
}

// MARK: - Controlling the Claude app

/// Self-test: the real Claude is not touched (CLAUDE_SWITCHER_FAKE_CLAUDE=1).
let fakeClaude = ProcessInfo.processInfo.environment["CLAUDE_SWITCHER_FAKE_CLAUDE"] != nil

enum Claude {
    static var running: NSRunningApplication? {
        if fakeClaude { return nil }
        return realRunning
    }
    static var realRunning: NSRunningApplication? {
        NSRunningApplication.runningApplications(withBundleIdentifier: claudeBundleID).first { !$0.isTerminated && isMainProcess($0) }
    }

    /// Whether this is the real Claude. macOS also files other programs launched from its sessions under the Claude id
    /// (for example, /usr/bin/osascript showing a notification), so we compare the executable itself.
    static func isMainProcess(_ app: NSRunningApplication) -> Bool {
        guard app.bundleIdentifier == claudeBundleID,
              let exe = app.executableURL,
              let main = app.bundleURL.flatMap({ Bundle(url: $0)?.executableURL }) else { return false }
        return exe.resolvingSymlinksInPath().path == main.resolvingSymlinksInPath().path
    }

    static var appURL: URL? {
        NSWorkspace.shared.urlForApplication(withBundleIdentifier: claudeBundleID)
            ?? (fm.fileExists(atPath: "/Applications/Claude.app") ? URL(fileURLWithPath: "/Applications/Claude.app") : nil)
    }

    /// Quits Claude and waits; if it did not quit, asks about force-quitting.
    @MainActor
    static func quit() async -> Bool {
        guard let app = running else { Log.write("quit: Claude is not running"); return true }
        Log.write("quit: quitting Claude (pid \(app.processIdentifier))")
        app.terminate()
        for _ in 0..<60 {                       // up to 30 seconds
            if app.isTerminated || running == nil { break }
            try? await Task.sleep(nanoseconds: 500_000_000)
        }
        if let still = running {
            let alert = NSAlert()
            alert.messageText = L("Claude did not quit", "Claude не закрылся")
            alert.informativeText = L("It may be waiting for an answer in one of its windows. Force quit it?",
                                      "Возможно, он ждёт ответа в своём окне. Закрыть принудительно?")
            alert.addButton(withTitle: L("Force quit", "Закрыть принудительно"))
            alert.addButton(withTitle: L("Cancel", "Отмена"))
            NSApp.activate(ignoringOtherApps: true)
            guard alert.runModal() == .alertFirstButtonReturn else { Log.write("quit: cancelled"); return false }
            Log.write("quit: forced")
            still.forceTerminate()
            for _ in 0..<20 where running != nil { try? await Task.sleep(nanoseconds: 250_000_000) }
        }
        // Let Electron finish writing files and release the LevelDB locks.
        try? await Task.sleep(nanoseconds: 1_000_000_000)
        Log.write("quit: \(running == nil ? "Claude closed" : "Claude still running")")
        return running == nil
    }

    static func launch() {
        if fakeClaude { Log.write("launch: (self-test) Claude would be launched"); return }
        guard let url = appURL else { Log.write("launch: Claude.app not found"); return }
        Log.write("launch: launching Claude")
        let cfg = NSWorkspace.OpenConfiguration(); cfg.activates = true
        NSWorkspace.shared.openApplication(at: url, configuration: cfg)
    }
}

// MARK: - VPN Happ
// Happ (com.happ.www) understands the links happ://connect, happ://disconnect, happ://close.
// Connected = the default route goes through utun and the xray core is running.

enum Happ {
    static let bundleID = "com.happ.www"

    static var installed: Bool { NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundleID) != nil }
    static var app: NSRunningApplication? {
        NSRunningApplication.runningApplications(withBundleIdentifier: bundleID).first { !$0.isTerminated }
    }

    static func run(_ path: String, _ args: [String]) -> (Int32, String) {
        let p = Process(); p.executableURL = URL(fileURLWithPath: path); p.arguments = args
        let out = Pipe(); p.standardOutput = out; p.standardError = FileHandle.nullDevice
        do { try p.run() } catch { return (-1, "") }
        let data = out.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        return (p.terminationStatus, String(decoding: data, as: UTF8.self))
    }

    static func defaultRouteInterface() -> String {
        let (_, out) = run("/sbin/route", ["-n", "get", "default"])
        for l in out.split(separator: "\n") where l.contains("interface:") {
            return l.split(separator: ":").last.map { $0.trimmingCharacters(in: .whitespaces) } ?? ""
        }
        return ""
    }

    static func isConnected() -> Bool {
        defaultRouteInterface().hasPrefix("utun") && run("/usr/bin/pgrep", ["-x", "xray"]).0 == 0
    }

    static func state() -> String {
        "Happ \(app == nil ? "closed" : "open"), route: \(defaultRouteInterface()), xray: \(run("/usr/bin/pgrep", ["-x", "xray"]).0 == 0 ? "yes" : "no")"
    }

    static func send(_ cmd: String) {
        Log.write("happ: opening happ://\(cmd)")
        let cfg = NSWorkspace.OpenConfiguration(); cfg.activates = false
        NSWorkspace.shared.open(URL(string: "happ://\(cmd)")!, configuration: cfg)
    }

    /// Checks that the internet really works through the tunnel: any HTTP response from api.anthropic.com.
    static func probe() async -> Bool {
        var req = URLRequest(url: URL(string: "https://api.anthropic.com/")!, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 6)
        req.httpMethod = "HEAD"
        let cfg = URLSessionConfiguration.ephemeral; cfg.timeoutIntervalForRequest = 6
        do { let (_, r) = try await URLSession(configuration: cfg).data(for: req); return r is HTTPURLResponse }
        catch { return false }
    }

    static func sleep(_ s: Double) async { try? await Task.sleep(nanoseconds: UInt64(s * 1e9)) }

    // --- external IP: through the VPN it must differ from the direct one; after Happ quits it is direct again.
    struct IPInfo { let ip: String; let place: String }

    static var directIP: String? {
        get { UserDefaults.standard.string(forKey: "directIP") }
        set { UserDefaults.standard.set(newValue, forKey: "directIP") }
    }
    static var vpnIP: String? {
        get { UserDefaults.standard.string(forKey: "vpnIP") }
        set { UserDefaults.standard.set(newValue, forKey: "vpnIP") }
    }

    static func publicIP() async -> IPInfo? {
        let cfg = URLSessionConfiguration.ephemeral; cfg.timeoutIntervalForRequest = 8
        let session = URLSession(configuration: cfg)
        for _ in 1...2 {
            if let (d, _) = try? await session.data(from: URL(string: "https://ipinfo.io/json")!),
               let o = try? JSONSerialization.jsonObject(with: d) as? [String: Any], let ip = o["ip"] as? String {
                let place = [o["country"], o["city"], o["org"]].compactMap { $0 as? String }.joined(separator: ", ")
                return IPInfo(ip: ip, place: place)
            }
            if let (d, _) = try? await session.data(from: URL(string: "https://api.ipify.org")!),
               let ip = String(data: d, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines), !ip.isEmpty, ip.count < 46 {
                return IPInfo(ip: ip, place: "?")
            }
            await sleep(1.5)
        }
        return nil
    }

    // --- the Happ service (happd, running as root) listens on the socket /tmp/happd.sock.
    // macOS (tmp_cleaner, every midnight) deletes everything in /tmp that has not been touched for 3 days. On 20.11.2026
    // the socket disappeared this way: the service was alive, but Happ said "Happ Client Service is unavailable. Please reinstall Happ".
    // Only root can restart the service, so we ask the LaunchDaemon local.happ-updater
    // (~/AccountChangeClaudeMac/happ-updater): it watches the repair-request file and restarts happd.
    static let daemonSocket = "/tmp/happd.sock"
    static let updaterDir = "/Users/Shared/HappUpdater"

    static var serviceAlive: Bool {
        var st = stat()
        return lstat(daemonSocket, &st) == 0 && (st.st_mode & S_IFMT) == S_IFSOCK
    }

    /// Check the Happ service: if the socket is there, refresh its timestamp so macOS does not delete it;
    /// if it is gone, ask the updater to restart the service and quit Happ so it connects to the new one.
    @discardableResult
    static func ensureService() async -> Bool {
        guard installed else { return false }
        var waited = 0
        while FileManager.default.fileExists(atPath: updaterDir + "/updating"), waited < 180 {
            if waited == 0 { Log.write("happ: the updater is installing a new Happ version, waiting") }
            await sleep(1); waited += 1
        }
        if serviceAlive { utimes(daemonSocket, nil); return true }
        Log.write("happ: service socket \(daemonSocket) is gone, asking happ-updater to restart the service")
        let req = updaterDir + "/repair-request"
        guard FileManager.default.createFile(atPath: req, contents: Data("\(Date())\n".utf8)) else {
            Log.write("happ: ERROR, could not write \(req): is the updater installed? (sudo bash ~/AccountChangeClaudeMac/happ-updater/install.sh)")
            return false
        }
        for _ in 0..<40 where !serviceAlive { await sleep(0.5) }    // up to 20 s
        guard serviceAlive else { Log.write("happ: ERROR, the Happ service did not come up"); return false }
        Log.write("happ: Happ service restarted")
        if let a = app {
            a.terminate()
            for _ in 0..<20 where !a.isTerminated { await sleep(0.25) }
            if !a.isTerminated { a.forceTerminate(); await sleep(1) }
            Log.write("happ: quit Happ so that it connects to the new service when it starts")
        }
        return true
    }

    /// Launches Happ (if it is not running), connects and waits until the tunnel works.
    static func connect() async -> Bool {
        await ensureService()
        Log.write("happ: connecting. Now: \(state())")
        if !isConnected() {
            if let d = await publicIP() { directIP = d.ip; Log.write("happ: direct IP before connecting: \(d.ip) (\(d.place))") }
            else { Log.write("happ: could not get the direct IP (no internet?)") }
            for attempt in 1...2 {
                send("connect")
                for _ in 0..<40 where !isConnected() { await sleep(0.5) }    // up to 20 s
                if isConnected() { break }
                Log.write("happ: attempt \(attempt) did not bring the tunnel up")
            }
        }
        guard isConnected() else { Log.write("happ: ERROR, did not connect. \(state())"); return false }
        var online = false
        for i in 1...5 {
            if await probe() { Log.write("happ: internet through the tunnel works (check \(i)). \(state())"); online = true; break }
            await sleep(1.5)
        }
        guard online else { Log.write("happ: ERROR, the tunnel is up but api.anthropic.com does not answer. \(state())"); return false }
        guard let v = await publicIP() else { Log.write("happ: ERROR, could not get the external IP through the tunnel"); return false }
        if let d = directIP, d == v.ip {
            Log.write("happ: ERROR, the external IP did not change (\(v.ip)): traffic bypasses the VPN"); return false
        }
        vpnIP = v.ip
        Log.write("happ: ALL OK. Happ is running and connected, internet works. IP via VPN: \(v.ip) (\(v.place)); direct IP: \(directIP ?? "unknown")")
        return true
    }

    /// Disconnects the VPN and fully quits Happ.
    @discardableResult
    static func shutdown() async -> Bool {
        guard app != nil || isConnected() else { Log.write("happ: already closed"); return true }
        Log.write("happ: disconnecting and quitting. Now: \(state())")
        if app != nil && isConnected() {
            send("disconnect")
            for _ in 0..<20 where isConnected() { await sleep(0.5) }         // up to 10 s
        }
        if app != nil {
            send("close")
            for _ in 0..<10 where app != nil { await sleep(0.5) }
        }
        if let a = app { Log.write("happ: did not quit on happ://close, terminating"); a.terminate()
            for _ in 0..<10 where app != nil { await sleep(0.5) } }
        if let a = app { a.forceTerminate(); await sleep(1) }
        guard app == nil, !isConnected() else { Log.write("happ: ERROR, Happ did not quit or the tunnel is still up. \(state())"); return false }
        // Wait until the network is direct again and check that the external IP is no longer the VPN one.
        var ip: IPInfo?
        for _ in 1...3 { ip = await publicIP(); if ip != nil { break }; await sleep(2) }
        guard let ip else { Log.write("happ: closed, no tunnel, but could not get the external IP (network still coming up?). \(state())"); return true }
        if let v = vpnIP, v == ip.ip { Log.write("happ: ERROR, after quitting the IP is still the VPN one (\(ip.ip))"); return false }
        directIP = ip.ip
        Log.write("happ: ALL OK. Happ fully quit, no tunnel, the IP is direct again: \(ip.ip) (\(ip.place))")
        return true
    }
}

// MARK: - Menu bar app

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    let profiles = Profiles()
    var statusItem: NSStatusItem!
    var busy = false
    var suppressLaunchPromptUntil = Date.distantPast
    var watcher: ClaudeLogWatcher!
    /// After a switch we wait to see whether the server accepts the login we put in place for this account.
    var expecting: (uuid: String, until: Date)?
    var autosaveWork: DispatchWorkItem?
    var declinedUuids = Set<String>()
    var promptOpen = false
    var lastAlert: String?

    func applicationDidFinishLaunching(_ n: Notification) {
        // usage/core.py (switch_timeline) parses this line: do not reword the start or "in Claude now: <uuid>".
        // Builds before November 2026 wrote it in Russian ("в Claude сейчас"); core.py accepts both forms.
        Log.write("=== Claude Switcher started. Accounts: \(profiles.store.accounts.map { "\($0.name)=\($0.accountUuid ?? "-")\($0.needsRelogin == true ? "(needs sign-in)" : "")" }.joined(separator: ", ")); in Claude now: \(profiles.currentUuid() ?? "nobody"); Claude \(Claude.running == nil ? "closed" : "open")")
        watcher = ClaudeLogWatcher { [weak self] e in self?.handle(e) }
        watcher.start()
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        let menu = NSMenu(); menu.delegate = self; menu.autoenablesItems = false
        statusItem.menu = menu
        updateTooltip()

        NSWorkspace.shared.notificationCenter.addObserver(
            self, selector: #selector(appLaunched(_:)),
            name: NSWorkspace.didLaunchApplicationNotification, object: nil)

        // First launch: right away offer to save the account that is signed in.
        if profiles.store.accounts.isEmpty, profiles.currentUuid() != nil {
            DispatchQueue.main.async { self.addCurrentAccount() }
        }
    }

    /// The app is reopened (from Spotlight / Finder / Dock): show the account picker.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showPicker(reason: Claude.running == nil ? L("Which account should Claude open?", "В какой аккаунт зайти?")
                                                  : L("Switch the Claude account?", "Переключить аккаунт Claude?"))
        return false
    }

    /// Draws the menu bar icon in the selected variant (and the current account's letter, if needed).
    func applyMenuIcon() {
        guard let b = statusItem?.button else { return }
        let icon = MenuIcon.find(profiles.store.menuIcon)
        b.image = icon.make()
        b.imagePosition = .imageLeading
        if icon.showsLetter, let cur = profiles.currentUuid().flatMap({ profiles.account(withUuid: $0) }),
           let ch = cur.name.first {
            b.attributedTitle = NSAttributedString(string: String(ch).uppercased(),
                attributes: [.font: NSFont.systemFont(ofSize: 13, weight: .semibold), .baselineOffset: 0.5])
        } else {
            b.title = ""
        }
    }

    func updateTooltip() {
        applyMenuIcon()
        let cur = profiles.currentUuid().flatMap { profiles.account(withUuid: $0) }
        statusItem?.button?.toolTip = "Claude: " + (cur?.name ?? L("account not saved", "аккаунт не сохранён"))
    }

    // MARK: menu

    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        let curUuid = profiles.currentUuid()
        let current = curUuid.flatMap { profiles.account(withUuid: $0) }

        let header = NSMenuItem(title: L("Claude accounts", "Аккаунты Claude"), action: nil, keyEquivalent: "")
        header.isEnabled = false
        menu.addItem(header)

        if profiles.store.accounts.isEmpty {
            let empty = NSMenuItem(title: L("No saved accounts yet", "Пока нет сохранённых аккаунтов"), action: nil, keyEquivalent: "")
            empty.isEnabled = false; menu.addItem(empty)
        }
        for (i, a) in profiles.store.accounts.enumerated() {
            let title = a.name + (a.useHapp == true ? "  · VPN" : "") + (a.needsRelogin == true ? L(" — sign in again", " — нужно войти заново") : "")
            let item = NSMenuItem(title: title, action: #selector(pickAccount(_:)), keyEquivalent: i < 9 ? "\(i + 1)" : "")
            item.target = self
            item.representedObject = a.id
            item.state = (a.id == current?.id) ? .on : .off
            item.isEnabled = !busy
            menu.addItem(item)
        }
        menu.addItem(.separator())

        if curUuid != nil && current == nil {
            add(menu, L("Save current account…", "Сохранить текущий аккаунт…"), #selector(addCurrentAccountAction))
        }
        add(menu, L("Sign in to a new account…", "Войти в новый аккаунт…"), #selector(addNewAccount))
        add(menu, L("Sign in to a new account via Happ VPN…", "Войти в новый аккаунт через VPN Happ…"), #selector(addNewAccountViaHapp)).isEnabled = !busy && Happ.installed

        if !profiles.store.accounts.isEmpty {
            let manage = NSMenuItem(title: L("Manage accounts", "Управление"), action: nil, keyEquivalent: "")
            let sub = NSMenu()
            for a in profiles.store.accounts {
                let m = NSMenuItem(title: a.name, action: nil, keyEquivalent: "")
                let s = NSMenu()
                let r = NSMenuItem(title: L("Rename…", "Переименовать…"), action: #selector(renameAccount(_:)), keyEquivalent: "")
                r.target = self; r.representedObject = a.id; s.addItem(r)
                let v = NSMenuItem(title: L("Connect Happ VPN", "Подключать VPN Happ"), action: #selector(toggleHapp(_:)), keyEquivalent: "")
                v.target = self; v.representedObject = a.id; v.state = a.useHapp == true ? .on : .off
                v.isEnabled = Happ.installed; s.addItem(v)
                let d = NSMenuItem(title: L("Remove from list…", "Удалить из списка…"), action: #selector(deleteAccount(_:)), keyEquivalent: "")
                d.target = self; d.representedObject = a.id; s.addItem(d)
                m.submenu = s; sub.addItem(m)
            }
            manage.submenu = sub; menu.addItem(manage)
        }
        menu.addItem(.separator())

        let iconItem = NSMenuItem(title: L("Menu bar icon", "Значок в строке меню"), action: nil, keyEquivalent: "")
        let iconMenu = NSMenu()
        for icon in MenuIcon.all {
            let i = NSMenuItem(title: icon.title, action: #selector(pickMenuIcon(_:)), keyEquivalent: "")
            i.target = self; i.representedObject = icon.id
            i.image = icon.make()
            i.state = icon.id == MenuIcon.find(profiles.store.menuIcon).id ? .on : .off
            iconMenu.addItem(i)
        }
        iconItem.submenu = iconMenu; menu.addItem(iconItem)

        let ask = add(menu, L("Ask for an account when Claude starts", "Спрашивать аккаунт при запуске Claude"), #selector(toggleAsk))
        ask.state = profiles.store.askOnClaudeLaunch ? .on : .off
        let login = add(menu, L("Open at login", "Запускать при входе в систему"), #selector(toggleLogin))
        login.state = SMAppService.mainApp.status == .enabled ? .on : .off
        add(menu, L("Open profiles folder", "Открыть папку с профилями"), #selector(openStore))
        add(menu, L("Open log", "Открыть журнал"), #selector(openLog))
        menu.addItem(.separator())
        add(menu, L("Quit Claude Switcher", "Выйти из Claude Switcher"), #selector(quitSelf), key: "q")
    }

    @discardableResult
    func add(_ menu: NSMenu, _ title: String, _ sel: Selector, key: String = "") -> NSMenuItem {
        let i = NSMenuItem(title: title, action: sel, keyEquivalent: key)
        i.target = self; i.isEnabled = !busy; menu.addItem(i); return i
    }

    // MARK: actions

    @objc func pickAccount(_ sender: NSMenuItem) {
        guard let id = sender.representedObject as? String,
              let a = profiles.store.accounts.first(where: { $0.id == id }) else { return }
        Task { await switchTo(a) }
    }

    @objc func addCurrentAccountAction() { addCurrentAccount() }

    func addCurrentAccount() {
        guard profiles.currentUuid() != nil else {
            alert(L("No one is signed in to Claude", "Сейчас в Claude никто не вошёл"),
                  L("Sign in to an account in the Claude app and try again.", "Войдите в аккаунт в приложении Claude и повторите.")); return
        }
        if let existing = profiles.currentUuid().flatMap({ profiles.account(withUuid: $0) }) {
            do { try profiles.saveCurrent(name: nil) } catch { alert(L("Could not save", "Не удалось сохранить"), error.localizedDescription) }
            alert(L("Already saved", "Уже сохранён"),
                  L("This account is already in the list as “\(existing.name)”. Its sign-in data has been updated.",
                    "Этот аккаунт уже есть в списке как «\(existing.name)». Данные входа обновлены."))
            return
        }
        guard let (name, vpn) = askNewAccount(L("What should this account be called?", "Как назвать этот аккаунт?"),
                                                 L("For example, the email you signed in to Claude with.", "Например, почта, с которой вы вошли в Claude.")) else { return }
        do {
            try profiles.saveCurrent(name: name)
            setHapp(name: name, vpn)
            Log.write("saved manually: “\(name)” \(profiles.currentUuid() ?? "-")")
            updateTooltip()
        } catch { Log.write("ERROR saving: \(error.localizedDescription)"); alert(L("Could not save", "Не удалось сохранить"), error.localizedDescription) }
    }

    /// Quit Claude, save the current account, clear the login and open Claude on the sign-in screen.
    @objc func addNewAccount() { startNewAccount(viaHapp: false) }
    @objc func addNewAccountViaHapp() { startNewAccount(viaHapp: true) }

    func startNewAccount(viaHapp: Bool) {
        Task {
            guard !busy else { return }
            Log.write("new account sign-in\(viaHapp ? " via Happ VPN" : ""): starting")
            busy = true; defer { busy = false }
            autosaveWork?.cancel()
            let leaving = profiles.currentUuid().flatMap { profiles.account(withUuid: $0) } ?? profiles.active
            guard await Claude.quit() else { return }
            if viaHapp {
                guard await Happ.connect() else {
                    alert(L("Happ did not connect", "Happ не подключился"),
                          L("Signing in to a new account was cancelled so that you do not sign in without the VPN. Details are in the log.",
                            "Вход в новый аккаунт отменён, чтобы не входить без VPN. Подробности в журнале."))
                    if leaving?.useHapp != true { Claude.launch() }; return
                }
            } else if Happ.app != nil || Happ.isConnected() {
                guard await Happ.shutdown() else {
                    alert(L("Happ did not quit", "Happ не закрылся"),
                          L("Signing in to a new account was stopped so that the account is not tied to the VPN IP. Quit Happ manually.",
                            "Вход в новый аккаунт остановлен, чтобы он не был связан с IP VPN. Закройте Happ вручную."))
                    return
                }
            }
            do {
                try profiles.initialBackupIfNeeded()
                try saveCurrentBeforeLeaving()
                try profiles.restore(nil)
                profiles.store.activeId = nil; profiles.save()
            } catch {
                Log.write("ERROR signing in to a new account: \(error.localizedDescription)")
                alert(L("Error", "Ошибка"), error.localizedDescription); Claude.launch(); return
            }
            suppressLaunchPromptUntil = Date().addingTimeInterval(20)
            Claude.launch()
            updateTooltip()
        }
    }

    /// Before leaving an account we save its fresh tokens (if the account is unknown, we ask for a name).
    func saveCurrentBeforeLeaving() throws {
        guard let uuid = profiles.currentUuid() else { return }
        if profiles.account(withUuid: uuid) != nil { try profiles.saveCurrent(name: nil); return }
        if let name = ask(L("Save the current account?", "Сохранить текущий аккаунт?"),
                          L("Claude is signed in to an account that is not in the list. What should it be called? (Cancel: do not save.)",
                            "В Claude выполнен вход в аккаунт, которого нет в списке. Как его назвать? (Отмена — не сохранять.)"),
                          defaultValue: "") {
            try profiles.saveCurrent(name: name)
        }
    }

    func switchTo(_ target: Account) async {
        guard !busy else { Log.write("switch to “\(target.name)” rejected: another one is in progress"); return }
        let curUuid = profiles.currentUuid()
        Log.write("switch: “\(target.name)” (\(target.accountUuid ?? "-")), in Claude now \(curUuid ?? "nobody")")
        // Taken before the first await: while Happ's service is being checked the main actor is free,
        // and a second switch (or an autosave) must not start in that gap.
        busy = true; defer { busy = false }
        await Happ.ensureService()
        let dead = target.needsRelogin == true
        let needVPN = target.useHapp == true
        if curUuid != nil, curUuid == target.accountUuid {
            if needVPN, !Happ.isConnected() {
                Log.write("switch: this is the current account, but Happ is not connected; reconnecting")
                if Claude.running != nil { guard await Claude.quit() else { return } }
                guard await Happ.connect() else {
                    alert(L("Happ did not connect", "Happ не подключился"),
                          L("Claude was not opened so that you do not sign in to “\(target.name)” without the VPN. Connect Happ manually and choose the account again. Details are in the log.",
                            "Claude не открыт, чтобы не входить в «\(target.name)» без VPN. Подключите Happ вручную и выберите аккаунт ещё раз. Подробности в журнале."))
                    return
                }
                suppressLaunchPromptUntil = Date().addingTimeInterval(20)
                Claude.launch(); return
            }
            Log.write("switch: this is already the current account")
            if Claude.running == nil { suppressLaunchPromptUntil = Date().addingTimeInterval(20); Claude.launch() }
            else { Claude.running?.activate() }
            return
        }
        // No questions: Claude quits right away and opens in the selected account.
        // If the saved login is stale, Claude opens on the sign-in screen, and after sign-in the switcher saves it by itself.
        if dead { Log.write("switch: the saved sign-in for “\(target.name)” is stale, opening the sign-in screen") }
        autosaveWork?.cancel()
        guard await Claude.quit() else { Log.write("switch aborted: Claude did not quit"); return }
        let leaving = curUuid.flatMap { profiles.account(withUuid: $0) } ?? profiles.active
        if needVPN {
            guard Happ.installed else {
                Log.write("ERROR: Happ is not installed")
                alert(L("Happ not found", "Happ не найден"),
                      L("“\(target.name)” needs the Happ VPN, but the app is not in /Applications.",
                        "Для «\(target.name)» нужен VPN Happ, но приложения нет в /Applications."))
                if leaving?.useHapp != true { Claude.launch() }; return
            }
            guard await Happ.connect() else {
                alert(L("Happ did not connect", "Happ не подключился"),
                      L("Switching to “\(target.name)” was cancelled so that you do not sign in without the VPN. Details are in the log (menu → “Open log”).",
                        "Переключение на «\(target.name)» отменено, чтобы не входить без VPN. Подробности в журнале (меню → «Открыть журнал»)."))
                if leaving?.useHapp != true { suppressLaunchPromptUntil = Date().addingTimeInterval(20); Claude.launch() }
                return
            }
        } else if leaving?.useHapp == true || (Happ.app != nil && profiles.store.accounts.contains { $0.useHapp == true }) {
            // Leaving a VPN account (or Happ stayed open after one): quit Happ and make sure the IP is direct again.
            guard await Happ.shutdown() else {
                alert(L("Happ did not quit", "Happ не закрылся"),
                      L("Switching to “\(target.name)” was stopped so that this account does not sign in from the VPN IP. Quit Happ manually and choose the account again.",
                        "Переключение на «\(target.name)» остановлено, чтобы этот аккаунт не зашёл с IP VPN. Закройте Happ вручную и выберите аккаунт ещё раз."))
                return
            }
        }
        Log.write("switch: Claude closed (\(Claude.running == nil ? "verified" : "NO!")), network ready, putting the files of “\(target.name)” in place")
        var restoreStarted = false
        do {
            try profiles.initialBackupIfNeeded()
            try saveCurrentBeforeLeaving()
            restoreStarted = true
            try profiles.restore(dead ? nil : target)
            profiles.store.activeId = target.id; profiles.save()
        } catch {
            Log.write("ERROR switching: \(error.localizedDescription)")
            // A restore that failed halfway leaves files of two accounts in the Claude folder: put the previous account back.
            var rolledBack = !restoreStarted
            if restoreStarted {
                do {
                    try profiles.restore(leaving)
                    rolledBack = true
                    Log.write("switch: the files of “\(leaving?.name ?? "empty login")” are back in place")
                } catch let rollbackError {
                    Log.write("ERROR rolling back: \(rollbackError.localizedDescription)")
                }
            }
            // The network is already set up for the target, so Claude reopens only if the previous account
            // lives on the same side of the VPN. Otherwise it stays closed: never a mixed login, never the wrong IP.
            let sameSide = (leaving?.useHapp == true) == needVPN
            let reopen = rolledBack && sameSide
            alert(L("Could not switch", "Не удалось переключить"),
                  error.localizedDescription + (reopen ? "" : L("\n\nClaude was not opened. Choose the account again.",
                                                               "\n\nClaude не открыт. Выберите аккаунт ещё раз.")))
            if reopen { suppressLaunchPromptUntil = Date().addingTimeInterval(20); Claude.launch() }
            updateTooltip()
            return
        }
        if !dead, let u = target.accountUuid {
            expecting = (u, Date().addingTimeInterval(120))
            DispatchQueue.main.asyncAfter(deadline: .now() + 120) { [weak self] in
                MainActor.assumeIsolated {
                    guard let self, self.expecting?.uuid == u else { return }
                    self.expecting = nil
                    Log.write("switch confirmed: Claude did not sign out of “\(target.name)” within 2 minutes, config.json has \(self.profiles.currentUuid() ?? "nobody")")
                }
            }
        }
        suppressLaunchPromptUntil = Date().addingTimeInterval(20)
        Claude.launch()
        updateTooltip()
        Log.write("switch: the files of “\(target.name)” are in place, Claude launched")
    }

    @objc func toggleHapp(_ sender: NSMenuItem) {
        guard let id = sender.representedObject as? String,
              let i = profiles.store.accounts.firstIndex(where: { $0.id == id }) else { return }
        let on = profiles.store.accounts[i].useHapp != true
        profiles.store.accounts[i].useHapp = on ? true : nil; profiles.save()
        Log.write("Happ VPN for “\(profiles.store.accounts[i].name)”: \(on ? "on" : "off")")
    }

    @objc func renameAccount(_ sender: NSMenuItem) {
        guard let id = sender.representedObject as? String,
              let i = profiles.store.accounts.firstIndex(where: { $0.id == id }) else { return }
        if let name = ask(L("New name", "Новое имя"), "", defaultValue: profiles.store.accounts[i].name) {
            profiles.store.accounts[i].name = name; profiles.save(); updateTooltip()
        }
    }

    @objc func deleteAccount(_ sender: NSMenuItem) {
        guard let id = sender.representedObject as? String,
              let a = profiles.store.accounts.first(where: { $0.id == id }) else { return }
        if confirm(L("Remove “\(a.name)” from the list?", "Удалить «\(a.name)» из списка?"),
                   L("The saved sign-in will be deleted from the switcher. Nothing changes in Claude itself.",
                     "Сохранённый вход будет удалён из переключателя. В самом Claude ничего не изменится."),
                   ok: L("Remove", "Удалить")) {
            profiles.remove(a); updateTooltip()
        }
    }

    @objc func pickMenuIcon(_ sender: NSMenuItem) {
        guard let id = sender.representedObject as? String else { return }
        profiles.store.menuIcon = id; profiles.save(); applyMenuIcon()
    }

    @objc func toggleAsk() { profiles.store.askOnClaudeLaunch.toggle(); profiles.save() }

    @objc func toggleLogin() {
        do {
            if SMAppService.mainApp.status == .enabled { try SMAppService.mainApp.unregister() }
            else { try SMAppService.mainApp.register() }
        } catch { alert(L("That did not work", "Не получилось"), error.localizedDescription) }
    }

    @objc func openStore() {
        try? fm.createDirectory(at: storeDir, withIntermediateDirectories: true)
        NSWorkspace.shared.open(storeDir)
    }

    @objc func openLog() {
        if !fm.fileExists(atPath: Log.file.path) { Log.write("log created") ; Thread.sleep(forTimeInterval: 0.2) }
        NSWorkspace.shared.open(Log.file)
    }

    @objc func quitSelf() { Log.write("quitting Claude Switcher"); NSApp.terminate(nil) }

    // MARK: Claude sign-in/sign-out events

    func handle(_ e: ClaudeAuthEvent) {
        switch e {
        case .loggedOut(let from):
            autosaveWork?.cancel()
            guard !busy, let from, let a = profiles.account(withUuid: from) else { return }
            let afterSwitch = expecting.map { $0.uuid == from && Date() < $0.until } ?? false
            expecting = nil
            profiles.setNeedsRelogin(uuid: from, true)
            updateTooltip()
            if afterSwitch {
                Log.write("ERROR: the server did not accept the saved sign-in for “\(a.name)”, Claude showed the sign-in screen")
                alert(L("The saved sign-in for “\(a.name)” no longer works", "Сохранённый вход «\(a.name)» больше не действует"),
                      L("Claude opened on the sign-in screen: the server rejected the saved data. This happens if someone logged out of this account in Claude itself: logging out ends the sign-in on the server.\n\nSign in to “\(a.name)” in the Claude window. A minute after you sign in, the switcher saves it again by itself.",
                        "Claude открылся на экране входа: сервер отклонил сохранённые данные. Так бывает, если в самом Claude нажимали «Выйти» из этого аккаунта: выход отменяет вход на сервере.\n\nВойдите в «\(a.name)» в окне Claude. Через минуту после входа переключатель сам сохранит его заново."))
            } else {
                Log.write("logged out of “\(a.name)” in Claude itself: the saved sign-in of this account is now invalid")
                alert(L("You logged out of “\(a.name)” in Claude itself", "Вы вышли из «\(a.name)» в самом Claude"),
                      L("Logging out in Claude ends the sign-in on the server, so the saved copy of “\(a.name)” will not open any more. To get this account back, sign in to it again: the switcher saves the sign-in by itself.\n\nTo change accounts without logging out of the current one, use the switcher menu: “Sign in to a new account…” or pick an account.",
                        "Выход в Claude отменяет вход на сервере, поэтому сохранённая копия «\(a.name)» больше не откроется. Чтобы вернуть этот аккаунт, войдите в него снова: переключатель сохранит вход сам.\n\nЧтобы сменить аккаунт, не выходя из текущего, пользуйтесь меню переключателя: «Войти в новый аккаунт…» или выбор аккаунта."))
            }
        case .loggedIn(_, let to):
            expecting = nil
            scheduleAutosave(uuid: to)
        case .forbidden:
            break
        }
    }

    /// 45 s after sign-in we save the login (Chromium does not flush cookies to disk right away, roughly every 30 s).
    func scheduleAutosave(uuid: String) {
        autosaveWork?.cancel()
        Log.write("signed in to Claude: \(uuid); saving the sign-in in 45 s")
        let w = DispatchWorkItem { [weak self] in MainActor.assumeIsolated { self?.autosave(uuid: uuid) } }
        autosaveWork = w
        DispatchQueue.main.asyncAfter(deadline: .now() + 45, execute: w)
    }

    func autosave(uuid: String) {
        guard !busy else { Log.write("autosave skipped: a switch is in progress"); return }
        guard profiles.currentUuid() == uuid else {
            Log.write("autosave skipped: config.json now has \(profiles.currentUuid() ?? "nobody"), expected \(uuid)"); return
        }
        do {
            if let a = profiles.account(withUuid: uuid) {
                try profiles.saveCurrent(name: nil)
                Log.write("autosave: sign-in for “\(a.name)” updated")
            } else {
                guard !declinedUuids.contains(uuid), !promptOpen else { return }
                promptOpen = true; defer { promptOpen = false }
                if let (name, vpn) = askNewAccount(L("Claude is signed in to a new account", "В Claude выполнен вход в новый аккаунт"),
                                  L("It is not in the switcher's list. What should it be called? (Cancel: do not save.)",
                                    "Его нет в списке переключателя. Как его назвать? (Отмена — не сохранять.)")) {
                    try profiles.saveCurrent(name: name)
                    setHapp(name: name, vpn)
                    Log.write("autosave: added account “\(name)” \(uuid)")
                } else {
                    declinedUuids.insert(uuid); Log.write("autosave: the user declined to save \(uuid)")
                }
            }
        } catch {
            Log.write("ERROR in autosave: \(error.localizedDescription)")
            alert(L("Could not save the sign-in", "Не удалось сохранить вход"), error.localizedDescription)
        }
        updateTooltip()
    }

    // MARK: account picker when Claude launches

    @objc func appLaunched(_ n: Notification) {
        guard let app = n.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication,
              app.bundleIdentifier == claudeBundleID else { return }
        guard Claude.isMainProcess(app) else {
            Log.write("launch under the Claude id ignored: not Claude but \(app.executableURL?.path ?? "an unknown file") (pid \(app.processIdentifier))")
            return
        }
        guard !busy, Date() > suppressLaunchPromptUntil else { return }
        if profiles.store.askOnClaudeLaunch, profiles.store.accounts.count >= 2,
           showPicker(reason: L("Claude is starting. Which account should it open?", "Claude запускается. В какой аккаунт зайти?")) { return }
        // Claude was opened manually in an account that needs the VPN, and Happ is not connected: quit, connect, reopen.
        if let cur = profiles.currentUuid().flatMap({ profiles.account(withUuid: $0) }), cur.useHapp == true, !Happ.isConnected() {
            Log.write("Claude opened in “\(cur.name)” without the VPN: quitting, connecting Happ, reopening")
            Task { await switchTo(cur) }
        }
    }

    /// Returns true if an account was chosen (the switch has started).
    @discardableResult
    func showPicker(reason: String) -> Bool {
        guard !busy else { return false }
        let accounts = profiles.store.accounts
        guard !accounts.isEmpty else { statusItem.button?.performClick(nil); return false }
        Log.write("account picker: “\(reason)”")
        let curUuid = profiles.currentUuid()
        let alert = NSAlert()
        alert.messageText = reason
        alert.informativeText = curUuid.flatMap { profiles.account(withUuid: $0) }.map { L("Current: ", "Сейчас: ") + $0.name } ?? ""
        // The current account goes first (the default button, Enter).
        let ordered = accounts.sorted { ($0.accountUuid == curUuid ? 0 : 1) < ($1.accountUuid == curUuid ? 0 : 1) }
        for a in ordered { alert.addButton(withTitle: (a.accountUuid == curUuid ? "✓ " : "") + a.name + (a.useHapp == true ? " · VPN" : "")) }
        alert.addButton(withTitle: L("Cancel", "Отмена"))
        alert.window.level = .floating
        NSApp.activate(ignoringOtherApps: true)
        let r = alert.runModal().rawValue - NSApplication.ModalResponse.alertFirstButtonReturn.rawValue
        guard r >= 0, r < ordered.count else { return false }
        Task { await switchTo(ordered[r]) }
        return true
    }

    // MARK: dialogs

    func alert(_ title: String, _ text: String) {
        if fakeClaude { Log.write("ALERT: \(title) — \(text)"); lastAlert = title; return }
        let a = NSAlert(); a.messageText = title; a.informativeText = text
        NSApp.activate(ignoringOtherApps: true); a.runModal()
    }

    func confirm(_ title: String, _ text: String, ok: String) -> Bool {
        let a = NSAlert(); a.messageText = title; a.informativeText = text
        a.addButton(withTitle: ok); a.addButton(withTitle: L("Cancel", "Отмена"))
        NSApp.activate(ignoringOtherApps: true)
        return a.runModal() == .alertFirstButtonReturn
    }

    /// Name of the new account and the "Connect VPN Happ" checkbox (checked by default if Happ is connected now).
    func askNewAccount(_ title: String, _ text: String) -> (String, Bool)? {
        let a = NSAlert(); a.messageText = title; a.informativeText = text
        let field = NSTextField(frame: NSRect(x: 0, y: 30, width: 300, height: 24))
        field.placeholderString = "name@example.com"
        let box = NSButton(checkboxWithTitle: L("Connect Happ VPN for this account", "Подключать VPN Happ для этого аккаунта"), target: nil, action: nil)
        box.frame = NSRect(x: 0, y: 0, width: 300, height: 22)
        box.state = Happ.installed && Happ.isConnected() ? .on : .off
        box.isEnabled = Happ.installed
        let v = NSView(frame: NSRect(x: 0, y: 0, width: 300, height: 56)); v.addSubview(field); v.addSubview(box)
        a.accessoryView = v
        a.addButton(withTitle: L("Save", "Сохранить")); a.addButton(withTitle: L("Cancel", "Отмена"))
        a.window.initialFirstResponder = field
        NSApp.activate(ignoringOtherApps: true)
        guard a.runModal() == .alertFirstButtonReturn else { return nil }
        let name = field.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        return name.isEmpty ? nil : (name, box.state == .on)
    }

    func setHapp(name: String, _ on: Bool) {
        guard let uuid = profiles.currentUuid(), let i = profiles.store.accounts.firstIndex(where: { $0.accountUuid == uuid }) else { return }
        profiles.store.accounts[i].useHapp = on ? true : nil; profiles.save()
        Log.write("Happ VPN for “\(name)”: \(on ? "on" : "off") (when saving)")
    }

    func ask(_ title: String, _ text: String, defaultValue: String) -> String? {
        let a = NSAlert(); a.messageText = title; a.informativeText = text
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 280, height: 24))
        field.stringValue = defaultValue; field.placeholderString = "name@example.com"
        a.accessoryView = field
        a.addButton(withTitle: L("Save", "Сохранить")); a.addButton(withTitle: L("Cancel", "Отмена"))
        a.window.initialFirstResponder = field
        NSApp.activate(ignoringOtherApps: true)
        guard a.runModal() == .alertFirstButtonReturn else { return nil }
        let v = field.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        return v.isEmpty ? nil : v
    }
}

// MARK: - Command line (for testing and initial setup)
//   ClaudeSwitcher list
//   ClaudeSwitcher add-current <name>     save the current login under a name
//   ClaudeSwitcher restore <name>         put a login in place (only while Claude is closed)
//   ClaudeSwitcher set-happ <name> on|off connect Happ VPN for this account or not
//   ClaudeSwitcher happ-test [connect|service|shutdown|ip|state]   try the Happ steps on their own
//   ClaudeSwitcher render-menu-icons <dir> draw every menu bar icon variant into PNGs
//   ClaudeSwitcher selftest               switching self-test with fake accounts (uses the real Happ VPN)

let args = CommandLine.arguments
if args.count >= 3, args[1] == "render-menu-icons" {
    // Draws each variant on a "menu bar" strip (dark and light) into a @2x PNG.
    let dir = URL(fileURLWithPath: args[2])
    try? fm.createDirectory(at: dir, withIntermediateDirectories: true)
    for icon in MenuIcon.all {
        for (mode, bg, fg) in [("dark", NSColor(white: 0.16, alpha: 1), NSColor.white), ("light", NSColor(white: 0.93, alpha: 1), NSColor.black)] {
            let W = 56, H = 24, scale = 2
            let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: W * scale, pixelsHigh: H * scale, bitsPerSample: 8,
                                       samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                                       bytesPerRow: 0, bitsPerPixel: 0)!
            rep.size = NSSize(width: W, height: H)
            NSGraphicsContext.saveGraphicsState()
            NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
            bg.setFill(); NSRect(x: 0, y: 0, width: W, height: H).fill()
            guard let img = icon.make() else { NSGraphicsContext.restoreGraphicsState(); print("no such symbol:", icon.id); continue }
            let letter = icon.showsLetter ? NSAttributedString(string: "A", attributes: [.font: NSFont.systemFont(ofSize: 13, weight: .semibold), .foregroundColor: fg]) : nil
            let total = img.size.width + (letter.map { 3 + $0.size().width } ?? 0)
            let x = (CGFloat(W) - total) / 2, y = (CGFloat(H) - img.size.height) / 2
            let tinted = NSImage(size: img.size, flipped: false) { r in
                img.draw(in: r); fg.set(); r.fill(using: .sourceAtop); return true
            }
            tinted.draw(in: NSRect(x: x, y: y, width: img.size.width, height: img.size.height))
            if let letter { letter.draw(at: NSPoint(x: x + img.size.width + 3, y: (CGFloat(H) - letter.size().height) / 2)) }
            NSGraphicsContext.restoreGraphicsState()
            try? rep.representation(using: .png, properties: [:])!.write(to: dir.appendingPathComponent("\(icon.id)-\(mode).png"))
        }
    }
    exit(0)
}

// Self-test of switching with the real Happ, but with fake accounts in a separate folder.
// Run: ClaudeSwitcher selftest  (it sets CLAUDE_SWITCHER_* itself and relaunches itself)
if args.count >= 2, args[1] == "selftest" {
    let env = ProcessInfo.processInfo.environment
    if env["CLAUDE_SWITCHER_FAKE_CLAUDE"] == nil {
        let root = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("claude-switcher-selftest-\(UUID().uuidString.prefix(8))")
        let p = Process(); p.executableURL = URL(fileURLWithPath: CommandLine.arguments[0]); p.arguments = ["selftest"]
        var e = env
        e["CLAUDE_SWITCHER_FAKE_CLAUDE"] = "1"
        e["CLAUDE_SWITCHER_CLAUDE_DIR"] = root.appendingPathComponent("claude").path
        e["CLAUDE_SWITCHER_STORE_DIR"] = root.appendingPathComponent("store").path
        p.environment = e
        try! p.run(); p.waitUntilExit(); exit(p.terminationStatus)
    }
    try! fm.createDirectory(at: claudeDir, withIntermediateDirectories: true)
    let accts: [(String, String, Bool)] = [("A-no-VPN", "aaaaaaaa-0000-0000-0000-000000000001", false),
                                           ("B-no-VPN", "bbbbbbbb-0000-0000-0000-000000000002", false),
                                           ("C-VPN", "cccccccc-0000-0000-0000-000000000003", true)]
    func fakeLogin(_ uuid: String) {
        try! "cookies-\(uuid)".write(to: claudeDir.appendingPathComponent("Cookies"), atomically: true, encoding: .utf8)
        let cfg: [String: Any] = ["lastKnownAccountUuid": uuid, "oauth:tokenCache": "token-\(uuid)", "userThemeMode": "dark"]
        try! JSONSerialization.data(withJSONObject: cfg).write(to: configFile)
    }
    var failures = 0
    func check(_ ok: Bool, _ what: String) { print(ok ? "  ✓" : "  ✗ FAILED:", what); if !ok { failures += 1 } }
    print("Self-test. Folder: \(claudeDir.deletingLastPathComponent().path)")
    MainActor.assumeIsolated {
        let d = AppDelegate()
        for (name, uuid, vpn) in accts {
            fakeLogin(uuid); try! d.profiles.saveCurrent(name: name)
            if vpn { d.profiles.store.accounts[d.profiles.store.accounts.count - 1].useHapp = true; d.profiles.save() }
        }
        // Start as in real life: account A is in Claude.
        try! d.profiles.restore(d.profiles.store.accounts[0])
        let order = ["C-VPN", "B-no-VPN", "C-VPN", "A-no-VPN", "B-no-VPN", "C-VPN", "A-no-VPN"]
        var done = false
        Task { @MainActor in
            if Happ.isConnected() { print("Happ is connected before the start: quitting it to begin with an account without the VPN"); await Happ.shutdown() }
            let direct = await Happ.publicIP()
            print("Direct IP: \(direct?.ip ?? "?") (\(direct?.place ?? ""))")
            for name in order {
                let t = d.profiles.store.accounts.first { $0.name == name }!
                print("→ switching to \(name)")
                d.lastAlert = nil
                await d.switchTo(t)
                let cfg = d.profiles.readConfig()
                check(d.lastAlert == nil, "no error alerts\(d.lastAlert.map { ": " + $0 } ?? "")")
                check(cfg["lastKnownAccountUuid"] as? String == t.accountUuid, "Claude has the login of \(name)")
                check(cfg["oauth:tokenCache"] as? String == "token-\(t.accountUuid!)", "the token belongs to this very account")
                check(cfg["userThemeMode"] as? String == "dark", "shared Claude settings untouched")
                let ip = await Happ.publicIP()
                if t.useHapp == true {
                    check(Happ.isConnected(), "Happ connected (route via utun, xray running)")
                    check(ip != nil && ip?.ip != direct?.ip, "external IP via the VPN: \(ip?.ip ?? "?") (\(ip?.place ?? ""))")
                } else {
                    check(Happ.app == nil, "Happ fully quit")
                    check(!Happ.isConnected(), "no tunnel")
                    check(ip != nil && ip?.ip == direct?.ip, "external IP is direct: \(ip?.ip ?? "?") (\(ip?.place ?? ""))")
                }
            }
            print(failures == 0 ? "RESULT: all passed, no failures" : "RESULT: \(failures) failures")
            done = true
        }
        while !done { RunLoop.main.run(until: Date().addingTimeInterval(0.1)) }
    }
    try? fm.removeItem(at: claudeDir.deletingLastPathComponent())
    exit(failures == 0 ? 0 : 1)
}

if args.count >= 2, ["list", "add-current", "restore", "set-happ", "happ-test"].contains(args[1]) {
    let p = Profiles()
    do {
        switch args[1] {
        case "list":
            print("current uuid:", p.currentUuid() ?? "-")
            for a in p.store.accounts { print(a.id == p.store.activeId ? "*" : " ", a.name, a.accountUuid ?? "-", a.useHapp == true ? "VPN Happ" : "") }
        case "set-happ":   // ClaudeSwitcher set-happ <name> on|off
            guard args.count >= 4, let i = p.store.accounts.firstIndex(where: { $0.name == args[2] }) else { print("usage: set-happ <name> on|off"); exit(2) }
            p.store.accounts[i].useHapp = args[3] == "on" ? true : nil; p.save()
            Log.write("Happ VPN for “\(args[2])”: \(args[3]) (command line)"); print("ok")
        case "happ-test":  // ClaudeSwitcher happ-test connect|shutdown|state
            let sem = DispatchSemaphore(value: 0)
            Task.detached {
                switch args.count >= 3 ? args[2] : "state" {
                case "connect": print("connect:", await Happ.connect())
                case "service": print("service:", await Happ.ensureService(), "| socket:", Happ.serviceAlive)
                case "shutdown": print("shutdown:", await Happ.shutdown())
                case "ip": let i = await Happ.publicIP(); print("ip:", i?.ip ?? "-", i?.place ?? "", "| direct:", Happ.directIP ?? "-", "vpn:", Happ.vpnIP ?? "-")
                default: print(Happ.state(), "connected:", Happ.isConnected())
                }
                sem.signal()
            }
            while sem.wait(timeout: .now()) == .timedOut { RunLoop.main.run(until: Date().addingTimeInterval(0.1)) }
        case "add-current":
            guard args.count >= 3 else { print("usage: add-current <name>"); exit(2) }
            if let a = try p.saveCurrent(name: args[2]) { print("saved:", a.name, a.accountUuid ?? "-") } else { print("no one is signed in to Claude"); exit(1) }
        case "restore":
            guard args.count >= 3, let a = p.store.accounts.first(where: { $0.name == args[2] }) else { print("no such account"); exit(2) }
            if Claude.running != nil && ProcessInfo.processInfo.environment["CLAUDE_SWITCHER_CLAUDE_DIR"] == nil { print("quit Claude first"); exit(1) }
            try p.initialBackupIfNeeded(); try p.restore(a); p.store.activeId = a.id; p.save(); print("restored:", a.name)
        default: break
        }
    } catch { print("error:", error.localizedDescription); exit(1) }
    exit(0)
}

MainActor.assumeIsolated {
    let app = NSApplication.shared
    let delegate = AppDelegate()
    app.delegate = delegate
    app.setActivationPolicy(.accessory)
    app.run()
}
