// Claude Switcher: a menu-bar account switcher for the Claude app (claude.ai desktop).
//
// How it works: the Claude login is stored in several files in ~/Library/Application Support/Claude
// (cookies, Local Storage, Session Storage, IndexedDB) and in several config.json keys
// (the OAuth token, which the Code tab also uses). For each account we keep a copy of these
// files. Switching: quit Claude -> save the current account -> put the target account's files
// in place -> launch Claude. Everything else (Code sessions, settings, caches, VM) is shared.

import AppKit
import CryptoKit
import ServiceManagement
import SQLite3

// MARK: - Environment hygiene
// Strip Electron / IDE / agent environment variables if Claude Switcher was launched from an editor or agent
// shell (e.g. `ELECTRON_RUN_AS_NODE=1` makes Electron apps like T3 Code and Claude Desktop exit immediately
// as headless Node, and a stale agent TMPDIR breaks `NSTemporaryDirectory()`). Keep `CLAUDE_SWITCHER_*`.
private let _envSanitized: Void = {
    let exact = Set(["ELECTRON_RUN_AS_NODE", "NODE_OPTIONS", "GEMINI_HOME", "BROWSER"])
    let prefixes = ["ELECTRON_", "T3CODE_", "T3_", "ANTIGRAVITY_", "AGY_", "CLAUDE_AGENT_SDK_"]
    for (k, v) in ProcessInfo.processInfo.environment {
        if k.hasPrefix("CLAUDE_SWITCHER_") { continue }
        if exact.contains(k) || prefixes.contains(where: { k.hasPrefix($0) }) {
            unsetenv(k)
        } else if k == "TMPDIR", (v.contains("/.t3/") || !FileManager.default.fileExists(atPath: v)) {
            unsetenv(k)
        }
    }
}()

// MARK: - Paths and constants

let claudeBundleID = "com.anthropic.claudefordesktop"
let t3BundleID = "com.t3tools.t3code"
let fm = FileManager.default
let home = fm.homeDirectoryForCurrentUser

let claudeDir: URL = {
    if let p = ProcessInfo.processInfo.environment["CLAUDE_SWITCHER_CLAUDE_DIR"] { return URL(fileURLWithPath: p) }
    return home.appendingPathComponent("Library/Application Support/Claude")
}()
let cliJsonFile: URL = {
    if let p = ProcessInfo.processInfo.environment["CLAUDE_SWITCHER_CLI_JSON"] { return URL(fileURLWithPath: p) }
    return home.appendingPathComponent(".claude.json")
}()
let cliKeychainFakeFile: URL? = {
    if let p = ProcessInfo.processInfo.environment["CLAUDE_SWITCHER_CLI_KEYCHAIN_FILE"] { return URL(fileURLWithPath: p) }
    return nil
}()
let t3StateDB: URL = {
    if let p = ProcessInfo.processInfo.environment["CLAUDE_SWITCHER_T3_DB"] { return URL(fileURLWithPath: p) }
    return home.appendingPathComponent(".t3/userdata/statev2.sqlite")
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
    var askBeforeRestartingBusyT3: Bool = true
    var didInitialBackup: Bool = false
    var didCliBackup: Bool = false
    var menuIcon: String = MenuIcon.defaultId

    init() {}
    init(from d: Decoder) throws {
        let c = try d.container(keyedBy: CodingKeys.self)
        accounts = try c.decodeIfPresent([Account].self, forKey: .accounts) ?? []
        activeId = try c.decodeIfPresent(String.self, forKey: .activeId)
        askOnClaudeLaunch = try c.decodeIfPresent(Bool.self, forKey: .askOnClaudeLaunch) ?? true
        askBeforeRestartingBusyT3 = try c.decodeIfPresent(Bool.self, forKey: .askBeforeRestartingBusyT3) ?? true
        didInitialBackup = try c.decodeIfPresent(Bool.self, forKey: .didInitialBackup) ?? false
        didCliBackup = try c.decodeIfPresent(Bool.self, forKey: .didCliBackup) ?? false
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
            // Preserve saved CLI account metadata when refreshing Desktop auth files.
            let existingCli = profileDir(a).appendingPathComponent("cli-account.json")
            if fm.fileExists(atPath: existingCli.path) {
                try fm.copyItem(at: existingCli, to: tmp.appendingPathComponent("cli-account.json"))
            }
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
        ClaudeCLI.removeSavedLogin(accountId: a.id)
        try? fm.removeItem(at: profileDir(a))
        store.accounts.removeAll { $0.id == a.id }
        if store.activeId == a.id { store.activeId = nil }
        save()
    }

    /// One-time backup of ~/.claude.json before the first CLI switch.
    func cliBackupIfNeeded() {
        guard !store.didCliBackup else { return }
        if fm.fileExists(atPath: cliJsonFile.path) {
            let f = DateFormatter(); f.dateFormat = "yyyy-MM-dd_HH-mm-ss"
            let dir = backupsDir.appendingPathComponent("cli-original-\(f.string(from: Date()))")
            try? fm.createDirectory(at: dir, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
            try? fm.copyItem(at: cliJsonFile, to: dir.appendingPathComponent("claude.json"))
            Log.write("cli backup: \(dir.path)")
        }
        store.didCliBackup = true
        save()
    }

    /// Saves the currently signed-in Claude CLI login into whichever profile matches its accountUuid.
    @discardableResult
    func syncLiveCliToMatchingProfile() -> Account? {
        guard let uuid = ClaudeCLI.currentUuid(), let a = account(withUuid: uuid) else { return nil }
        return ClaudeCLI.snapshotLive(into: a, profileDir: profileDir(a)) ? a : nil
    }

    func hasCliLogin(_ a: Account) -> Bool {
        ClaudeCLI.hasSavedLogin(accountId: a.id, profileDir: profileDir(a))
    }
}

// MARK: - Claude Code CLI auth (~/.claude.json + Keychain "Claude Code-credentials")
// Used by T3 Code and terminal `claude`. Only the account keys (`oauthAccount` in ~/.claude.json and
// `claudeAiOauth` inside Keychain "Claude Code-credentials") are swapped; shared settings and `mcpOAuth`
// tokens stay untouched. Keychain is accessed via `/usr/bin/security` so the ACL stays compatible with
// `claude` CLI without macOS prompts: stdin (`security -i`) is used for normal-sized payloads (<3500 hex
// chars, keeping tokens out of `ps`), with automatic fallback to direct `-X` when large `mcpOAuth` blobs
// exceed `security -i`'s 4096-byte line buffer.

enum ClaudeCLI {
    static let liveKeychainService = "Claude Code-credentials"
    static let keychainAccount = NSUserName()
    static func profileKeychainService(_ id: String) -> String { "ClaudeSwitcher-cli-\(id)" }

    static var binaryPath: String? {
        let candidates = [
            home.appendingPathComponent(".local/bin/claude").path,
            "/opt/homebrew/bin/claude",
            "/usr/local/bin/claude",
        ]
        for c in candidates where fm.isExecutableFile(atPath: c) { return c }
        let (rc, out) = Happ.run("/usr/bin/which", ["claude"])
        let p = out.trimmingCharacters(in: .whitespacesAndNewlines)
        return rc == 0 && !p.isEmpty && fm.isExecutableFile(atPath: p) ? p : nil
    }

    // --- Keychain helpers (fake JSON store in selftest, real macOS Keychain via /usr/bin/security otherwise)

    private static func readFakeKeychain() -> [String: String] {
        guard let url = cliKeychainFakeFile,
              let data = try? Data(contentsOf: url),
              let dict = try? JSONSerialization.jsonObject(with: data) as? [String: String] else { return [:] }
        return dict
    }

    private static func writeFakeKeychain(_ dict: [String: String]) throws {
        guard let url = cliKeychainFakeFile else { return }
        try fm.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        let data = try JSONSerialization.data(withJSONObject: dict, options: [.prettyPrinted])
        try data.write(to: url, options: .atomic)
    }

    static func readKeychain(service: String) -> String? {
        if cliKeychainFakeFile != nil { return readFakeKeychain()[service] }
        let (rc, out) = Happ.run("/usr/bin/security", ["find-generic-password", "-a", keychainAccount, "-s", service, "-w"])
        if rc == 0 {
            let trimmed = out.trimmingCharacters(in: .newlines)
            return trimmed.isEmpty ? nil : trimmed
        }
        // Fallback without -a in case an entry was stored under another account attribute.
        let (rc2, out2) = Happ.run("/usr/bin/security", ["find-generic-password", "-s", service, "-w"])
        guard rc2 == 0 else { return nil }
        let trimmed2 = out2.trimmingCharacters(in: .newlines)
        return trimmed2.isEmpty ? nil : trimmed2
    }

    static func writeKeychain(service: String, value: String) throws {
        if cliKeychainFakeFile != nil {
            var dict = readFakeKeychain()
            dict[service] = value
            try writeFakeKeychain(dict)
            return
        }
        let hex = Data(value.utf8).map { String(format: "%02x", $0) }.joined()
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/security")
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        if hex.count < 3500 {
            // Keep tokens out of process arguments when safely under `security -i`'s 4096-byte line buffer.
            let safeAcct = keychainAccount.replacingOccurrences(of: "\"", with: "")
            let safeSvc = service.replacingOccurrences(of: "\"", with: "")
            let script = "add-generic-password -U -a \"\(safeAcct)\" -s \"\(safeSvc)\" -X \(hex)\n"
            p.arguments = ["-i"]
            let inPipe = Pipe()
            p.standardInput = inPipe
            try p.run()
            inPipe.fileHandleForWriting.write(Data(script.utf8))
            try? inPipe.fileHandleForWriting.close()
        } else {
            // Large payloads (e.g. Claude Code-credentials with big mcpOAuth) exceed `security -i`'s 4096-byte buffer.
            p.arguments = ["add-generic-password", "-U", "-a", keychainAccount, "-s", service, "-X", hex]
            try p.run()
        }
        p.waitUntilExit()
        guard p.terminationStatus == 0 else {
            throw SwitchError.message(L("Keychain write failed for \(service) (code \(p.terminationStatus)).",
                                        "Не удалось записать в связку ключей \(service) (код \(p.terminationStatus))."))
        }
    }

    static func deleteKeychain(service: String) {
        if cliKeychainFakeFile != nil {
            var dict = readFakeKeychain()
            dict.removeValue(forKey: service)
            try? writeFakeKeychain(dict)
            return
        }
        _ = Happ.run("/usr/bin/security", ["delete-generic-password", "-a", keychainAccount, "-s", service])
    }

    // --- ~/.claude.json helpers

    static func readJson() -> [String: Any] {
        guard let data = try? Data(contentsOf: cliJsonFile),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return [:] }
        return obj
    }

    static func writeJson(_ obj: [String: Any]) throws {
        try fm.createDirectory(at: cliJsonFile.deletingLastPathComponent(), withIntermediateDirectories: true)
        let data = try JSONSerialization.data(withJSONObject: obj, options: [.prettyPrinted, .withoutEscapingSlashes])
        try data.write(to: cliJsonFile, options: .atomic)
        try? fm.setAttributes([.posixPermissions: 0o600], ofItemAtPath: cliJsonFile.path)
    }

    static func currentAccount() -> [String: Any]? {
        readJson()["oauthAccount"] as? [String: Any]
    }

    static func currentUuid() -> String? {
        currentAccount()?["accountUuid"] as? String
    }

    static func currentEmail() -> String? {
        currentAccount()?["emailAddress"] as? String
    }

    // --- Keychain JSON helpers (preserving mcpOAuth and other non-account keys)

    static func readLiveCredentialsDict() -> [String: Any] {
        guard let raw = readKeychain(service: liveKeychainService),
              let data = raw.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return [:] }
        return obj
    }

    static func readLiveOAuth() -> [String: Any]? {
        readLiveCredentialsDict()["claudeAiOauth"] as? [String: Any]
    }

    static func writeLiveOAuth(_ oauth: [String: Any]?) throws {
        var dict = readLiveCredentialsDict()
        if let oauth { dict["claudeAiOauth"] = oauth }
        else { dict.removeValue(forKey: "claudeAiOauth") }
        let data = try JSONSerialization.data(withJSONObject: dict, options: [.withoutEscapingSlashes])
        let str = String(decoding: data, as: UTF8.self)
        try writeKeychain(service: liveKeychainService, value: str)
    }

    static func writeLiveAccount(_ acct: [String: Any]?) throws {
        var obj = readJson()
        if let acct { obj["oauthAccount"] = acct }
        else { obj.removeValue(forKey: "oauthAccount") }
        try writeJson(obj)
    }

    // --- Snapshot and restore

    static func hasSavedLogin(accountId: String, profileDir: URL) -> Bool {
        let acctFile = profileDir.appendingPathComponent("cli-account.json")
        guard fm.fileExists(atPath: acctFile.path),
              let raw = readKeychain(service: profileKeychainService(accountId)),
              let data = raw.data(using: .utf8),
              let oauth = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let token = oauth["accessToken"] as? String, !token.isEmpty else { return false }
        return true
    }

    @discardableResult
    static func snapshotLive(into a: Account, profileDir: URL) -> Bool {
        guard let acct = currentAccount(),
              let uuid = acct["accountUuid"] as? String, !uuid.isEmpty else { return false }
        if let expected = a.accountUuid, expected != uuid { return false }
        guard let oauth = readLiveOAuth(),
              let token = oauth["accessToken"] as? String, !token.isEmpty else { return false }
        do {
            try fm.createDirectory(at: profileDir, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
            let acctData = try JSONSerialization.data(withJSONObject: acct, options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes])
            let acctFile = profileDir.appendingPathComponent("cli-account.json")
            let oauthData = try JSONSerialization.data(withJSONObject: oauth, options: [.sortedKeys, .withoutEscapingSlashes])
            let oauthStr = String(decoding: oauthData, as: UTF8.self)

            let existingAcct = try? Data(contentsOf: acctFile)
            let existingOauth = readKeychain(service: profileKeychainService(a.id))
            // Skip redundant disk/Keychain writes and log spam when nothing changed (e.g. menu open).
            if existingAcct == acctData && existingOauth == oauthStr { return true }

            if existingAcct != acctData {
                try acctData.write(to: acctFile, options: .atomic)
                try? fm.setAttributes([.posixPermissions: 0o600], ofItemAtPath: acctFile.path)
            }
            if existingOauth != oauthStr {
                try writeKeychain(service: profileKeychainService(a.id), value: oauthStr)
            }
            let email = acct["emailAddress"] as? String ?? "-"
            Log.write("cli snapshot “\(a.name)” uuid=\(uuid) email=\(email)")
            return true
        } catch {
            Log.write("ERROR cli snapshot “\(a.name)”: \(error.localizedDescription)")
            return false
        }
    }

    /// Restores the saved CLI login for the account, or clears the active CLI login if nil or unsaved.
    /// Returns true if a saved CLI login was put in place, false if CLI was cleared.
    @discardableResult
    static func restore(account a: Account?, profileDir: URL?) throws -> Bool {
        if let a, let profileDir {
            let acctFile = profileDir.appendingPathComponent("cli-account.json")
            if let acctData = try? Data(contentsOf: acctFile),
               let acct = try? JSONSerialization.jsonObject(with: acctData) as? [String: Any],
               let raw = readKeychain(service: profileKeychainService(a.id)),
               let oauthData = raw.data(using: .utf8),
               let oauth = try? JSONSerialization.jsonObject(with: oauthData) as? [String: Any],
               let token = oauth["accessToken"] as? String, !token.isEmpty {
                try writeLiveOAuth(oauth)
                try writeLiveAccount(acct)
                let uuid = acct["accountUuid"] as? String ?? a.accountUuid ?? "-"
                let email = acct["emailAddress"] as? String ?? "-"
                // usage/core.py (cli_switch_timeline) parses "cli restore done:" and "uuid=".
                Log.write("cli restore done: uuid=\(uuid) email=\(email) account=“\(a.name)”")
                return true
            }
        }
        try writeLiveOAuth(nil)
        try writeLiveAccount(nil)
        Log.write("cli restore cleared (\(a.map { "no saved CLI login for “\($0.name)”" } ?? "empty login"))")
        return false
    }

    static func removeSavedLogin(accountId: String) {
        deleteKeychain(service: profileKeychainService(accountId))
    }

    static func emailHint(for a: Account) -> String? {
        guard let first = a.name.split(whereSeparator: { $0.isWhitespace }).first, first.contains("@") else { return nil }
        return String(first)
    }

    /// Opens Terminal.app with an interactive `claude auth login` command that saves the token via `ClaudeSwitcher cli-save`
    /// and optionally relaunches T3 Code once sign-in finishes. Never blocks the menu bar UI.
    @discardableResult
    static func launchTerminalLogin(for target: Account, relaunchT3: Bool) -> Bool {
        guard let bin = binaryPath else {
            Log.write("ERROR cli login: claude binary not found")
            return false
        }
        let switcherBin = Bundle.main.executablePath ?? CommandLine.arguments[0]
        let email = emailHint(for: target)
        func shQuote(_ s: String) -> String { "'" + s.replacingOccurrences(of: "'", with: "'\\''") + "'" }
        let emailFlag = email.map { " --email \(shQuote($0))" } ?? ""
        let relaunchLine = relaunchT3 ? "\nenv -u ELECTRON_RUN_AS_NODE \(shQuote(switcherBin)) launch-t3 >/dev/null 2>&1 || true" : ""
        let script = """
        #!/bin/zsh
        clear
        echo "======================================================"
        echo "  Claude Code CLI / T3 Code sign-in:"
        echo "  \(target.name.replacingOccurrences(of: "\"", with: ""))"
        echo "======================================================"
        echo ""
        export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
        \(shQuote(bin)) auth login --claudeai\(emailFlag)
        rc=$?
        echo ""
        if [ $rc -eq 0 ]; then
          if \(shQuote(switcherBin)) cli-save; then\(relaunchLine)
            echo ""
            echo "✓ Done! Login saved to Claude Switcher. You can close this window."
          else\(relaunchLine)
            echo ""
            echo "⚠ Signed in, but Claude Switcher could not match/save the profile."
          fi
        else
          echo "✗ Sign-in did not finish (exit $rc)."\(relaunchLine)
        fi
        rm -f "$0"
        """
        let tmpDirPath = fm.fileExists(atPath: NSTemporaryDirectory()) ? NSTemporaryDirectory() : "/tmp"
        let tmp = URL(fileURLWithPath: tmpDirPath).appendingPathComponent("claude-cli-login-\(UUID().uuidString.prefix(8)).command")
        do {
            try script.write(to: tmp, atomically: true, encoding: .utf8)
            try fm.setAttributes([.posixPermissions: 0o700], ofItemAtPath: tmp.path)
            let p = Process()
            p.executableURL = URL(fileURLWithPath: "/usr/bin/open")
            p.arguments = ["-a", "Terminal", tmp.path]
            try p.run()
            Log.write("cli login: opened Terminal for “\(target.name)” (email hint: \(email ?? "-"))")
            return true
        } catch {
            Log.write("ERROR cli login: could not open Terminal: \(error.localizedDescription)")
            return false
        }
    }
}

// MARK: - Imposter-safe app launching (shared by Claude Desktop and T3 Code)
// macOS LaunchServices can file helper/child processes spawned inside an app (e.g. Playwright's
// chrome-headless-shell or osascript) under the parent app's bundle ID. If such a process outlives the
// real app as a foreground NSRunningApplication, plain `openApplication` / `open -a` / `open -b` merely
// focuses the ghost process instead of launching the real app. We classify every live instance, safely
// terminate strictly-verified orphaned headless ghosts (so they don't hijack Dock/Cmd+Tab), and bypass
// any remaining non-main imposters with `createsNewApplicationInstance = true` / `open -n -a`.

enum AppLaunch {
    struct InstanceInfo: Equatable {
        let pid: pid_t
        let bundleID: String
        let executablePath: String?
        let bundlePath: String?
        let mainExecutablePath: String?
        let isTerminated: Bool
        let activationPolicy: NSApplication.ActivationPolicy
        let ppid: pid_t?
        let uid: uid_t?
        let commandLine: String?
        let visibleWindowCount: Int?
    }

    enum InstanceRole: Equatable {
        case main
        case reapableOrphanHeadless
        case imposter
    }

    enum LaunchPlan: Equatable {
        case activateExisting(pid: pid_t)
        case launch(newInstance: Bool, reapPids: [pid_t], imposterPids: [pid_t])
    }

    struct Hooks {
        let snapshot: @MainActor () -> [InstanceInfo]
        let activate: @MainActor (pid_t) -> Void
        let reap: @MainActor (InstanceInfo) async -> Bool
        let open: @MainActor (_ newInstance: Bool) async throws -> pid_t?
        let openFallback: @MainActor (_ newInstance: Bool) -> Void
        let sleep: @MainActor (_ nanoseconds: UInt64) async -> Void
    }

    static func procMeta(_ pid: pid_t) -> (ppid: pid_t, uid: uid_t)? {
        var mib: [Int32] = [CTL_KERN, KERN_PROC, KERN_PROC_PID, pid]
        var info = kinfo_proc()
        var size = MemoryLayout<kinfo_proc>.size
        guard sysctl(&mib, UInt32(mib.count), &info, &size, nil, 0) == 0, size > 0 else { return nil }
        return (info.kp_eproc.e_ppid, info.kp_eproc.e_ucred.cr_uid)
    }

    static func commandLine(_ pid: pid_t) -> String? {
        let (rc, out) = Happ.run("/bin/ps", ["-p", String(pid), "-o", "command="])
        guard rc == 0 else { return nil }
        let trimmed = out.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }

    /// Count of normal-layer (layer 0) non-empty windows owned by `pid` across all spaces (including minimized).
    static func layerZeroWindowCount(_ pid: pid_t) -> Int {
        guard let list = CGWindowListCopyWindowInfo([.optionAll, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] else {
            return -1
        }
        var count = 0
        for w in list {
            guard (w[kCGWindowOwnerPID as String] as? Int32) == pid else { continue }
            let layer = (w[kCGWindowLayer as String] as? Int) ?? 0
            guard layer == 0 else { continue }
            if let bounds = w[kCGWindowBounds as String] as? [String: Any],
               let bw = bounds["Width"] as? Double, let bh = bounds["Height"] as? Double,
               bw <= 0 || bh <= 0 { continue }
            count += 1
        }
        return count
    }

    static func isHeadlessBrowser(executablePath: String, commandLine: String?) -> Bool {
        let lowerExe = executablePath.lowercased()
        let base = URL(fileURLWithPath: lowerExe).lastPathComponent
        if base == "chrome-headless-shell" || base == "headless_shell" { return true }
        if lowerExe.contains("/ms-playwright/") && (base.contains("headless") || base.contains("chrom")) { return true }
        if let cmd = commandLine?.lowercased(), cmd.contains("--headless"),
           (lowerExe.contains("chrom") || lowerExe.contains("playwright") || lowerExe.contains("puppeteer")) {
            return true
        }
        return false
    }

    static func isInsideBundle(executablePath: String, bundlePath: String?) -> Bool {
        guard let bundlePath, !bundlePath.isEmpty else { return false }
        let prefix = bundlePath.hasSuffix("/") ? bundlePath : bundlePath + "/"
        return executablePath.hasPrefix(prefix)
    }

    static func classify(_ info: InstanceInfo, expectedBundleID: String, currentUID: uid_t, excludedCmdSubstrings: [String] = []) -> InstanceRole {
        guard !info.isTerminated, info.bundleID == expectedBundleID else { return .imposter }
        if let exe = info.executablePath, let main = info.mainExecutablePath, exe == main,
           info.activationPolicy != .prohibited {
            let cmd = info.commandLine ?? ""
            if !excludedCmdSubstrings.contains(where: { cmd.contains($0) }) {
                return .main
            }
        }
        if info.activationPolicy == .regular,
           info.ppid == 1,
           info.uid == currentUID,
           info.visibleWindowCount == 0,
           let exe = info.executablePath,
           !isInsideBundle(executablePath: exe, bundlePath: info.bundlePath),
           isHeadlessBrowser(executablePath: exe, commandLine: info.commandLine) {
            return .reapableOrphanHeadless
        }
        return .imposter
    }

    static func planLaunch(instances: [InstanceInfo], expectedBundleID: String, currentUID: uid_t, excludedCmdSubstrings: [String] = []) -> LaunchPlan {
        let live = instances.filter { !$0.isTerminated && $0.bundleID == expectedBundleID }
        var reapPids: [pid_t] = []
        var imposterPids: [pid_t] = []
        for inst in live {
            switch classify(inst, expectedBundleID: expectedBundleID, currentUID: currentUID, excludedCmdSubstrings: excludedCmdSubstrings) {
            case .main:
                return .activateExisting(pid: inst.pid)
            case .reapableOrphanHeadless:
                reapPids.append(inst.pid)
            case .imposter:
                imposterPids.append(inst.pid)
            }
        }
        return .launch(newInstance: !imposterPids.isEmpty, reapPids: reapPids, imposterPids: imposterPids)
    }

    static func snapshotLive(bundleID: String, excludedCmdSubstrings: [String] = []) -> [InstanceInfo] {
        NSRunningApplication.runningApplications(withBundleIdentifier: bundleID).compactMap { app in
            guard !app.isTerminated else { return nil }
            let pid = app.processIdentifier
            let exe = app.executableURL?.resolvingSymlinksInPath().path
            let bundlePath = app.bundleURL?.resolvingSymlinksInPath().path
            let mainExe = app.bundleURL.flatMap { Bundle(url: $0)?.executableURL }?.resolvingSymlinksInPath().path
            let isSameExe = (exe != nil && exe == mainExe)
            // Only query proc/window/cmdline when needed (non-main imposters, or main with excluded substrings).
            let needCmd = !isSameExe || !excludedCmdSubstrings.isEmpty
            let meta = isSameExe ? nil : procMeta(pid)
            let windows = isSameExe ? nil : layerZeroWindowCount(pid)
            let cmd = needCmd ? commandLine(pid) : nil
            return InstanceInfo(
                pid: pid,
                bundleID: app.bundleIdentifier ?? "",
                executablePath: exe,
                bundlePath: bundlePath,
                mainExecutablePath: mainExe,
                isTerminated: app.isTerminated,
                activationPolicy: app.activationPolicy,
                ppid: meta?.ppid,
                uid: meta?.uid,
                commandLine: cmd,
                visibleWindowCount: windows
            )
        }
    }

    @MainActor
    static func reapOrphanLive(_ inst: InstanceInfo, expectedBundleID: String, label: String) async -> Bool {
        let myUID = getuid()
        // Re-verify ownership and identity right before sending SIGTERM.
        guard let liveApp = NSRunningApplication(processIdentifier: inst.pid),
              !liveApp.isTerminated,
              liveApp.bundleIdentifier == expectedBundleID,
              liveApp.activationPolicy == .regular,
              liveApp.executableURL?.resolvingSymlinksInPath().path == inst.executablePath,
              let meta = procMeta(inst.pid), meta.ppid == 1, meta.uid == myUID,
              layerZeroWindowCount(inst.pid) == 0 else {
            return false
        }
        let exeName = inst.executablePath.map { URL(fileURLWithPath: $0).lastPathComponent } ?? "?"
        Log.write("launch: terminating orphaned headless \(label) imposter (pid \(inst.pid), \(exeName), ppid=1, windows=0)")
        _ = Darwin.kill(inst.pid, SIGTERM)
        for _ in 0..<15 {
            if liveApp.isTerminated || procMeta(inst.pid) == nil {
                Log.write("launch: orphaned headless \(label) imposter (pid \(inst.pid)) exited")
                return true
            }
            try? await Task.sleep(nanoseconds: 100_000_000)
        }
        Log.write("launch: orphaned headless \(label) imposter (pid \(inst.pid)) still running after SIGTERM; will bypass with createsNewApplicationInstance")
        return false
    }

    @MainActor
    static func runLaunchLoop(
        label: String,
        bundleID: String,
        currentUID: uid_t = getuid(),
        excludedCmdSubstrings: [String] = [],
        canRetry: @escaping @MainActor () -> Bool = { true },
        onConfirmedPid: (@MainActor (pid_t) -> Void)? = nil,
        settleNanos: UInt64 = 1_000_000_000,
        hooks: Hooks
    ) async -> pid_t? {
        for attempt in 1...3 {
            guard !Task.isCancelled, canRetry() else { return nil }
            let initial = hooks.snapshot()
            let initialPlan = planLaunch(instances: initial, expectedBundleID: bundleID, currentUID: currentUID, excludedCmdSubstrings: excludedCmdSubstrings)
            switch initialPlan {
            case .activateExisting(let pid):
                hooks.activate(pid)
                onConfirmedPid?(pid)
                Log.write("launch: \(label) already running (pid \(pid)), activated")
                return pid
            case .launch(_, let reapPids, _):
                for pid in reapPids {
                    guard !Task.isCancelled, canRetry() else { return nil }
                    if let inst = initial.first(where: { $0.pid == pid }) {
                        _ = await hooks.reap(inst)
                    }
                }
            }

            guard !Task.isCancelled, canRetry() else { return nil }
            // Re-snapshot right before launching so we never spawn a duplicate if the real app appeared.
            let preLaunch = hooks.snapshot()
            let prePlan = planLaunch(instances: preLaunch, expectedBundleID: bundleID, currentUID: currentUID, excludedCmdSubstrings: excludedCmdSubstrings)
            let useNewInstance: Bool
            switch prePlan {
            case .activateExisting(let pid):
                hooks.activate(pid)
                onConfirmedPid?(pid)
                Log.write("launch: \(label) already running (pid \(pid)), activated")
                return pid
            case .launch(let needNew, let stillReapable, let imposters):
                let allRemaining = stillReapable + imposters
                useNewInstance = needNew || !allRemaining.isEmpty || attempt > 1
                if !allRemaining.isEmpty {
                    let desc = allRemaining.map { pid -> String in
                        let exe = preLaunch.first(where: { $0.pid == pid })?.executablePath.map { URL(fileURLWithPath: $0).lastPathComponent } ?? "?"
                        return "\(pid)(\(exe))"
                    }.joined(separator: ", ")
                    Log.write("launch: non-main \(label) imposter(s) present [\(desc)]; using createsNewApplicationInstance=true")
                }
            }

            do {
                let returnedPid = try await hooks.open(useNewInstance)
                await hooks.sleep(settleNanos)
                guard !Task.isCancelled, canRetry() else { return nil }
                let after = hooks.snapshot()
                let mains = after.filter {
                    classify($0, expectedBundleID: bundleID, currentUID: currentUID, excludedCmdSubstrings: excludedCmdSubstrings) == .main
                }
                if let confirmed = mains.first(where: { $0.pid == returnedPid }) ?? mains.first {
                    onConfirmedPid?(confirmed.pid)
                    Log.write("launch: \(label) launched (pid \(confirmed.pid), attempt \(attempt))")
                    return confirmed.pid
                }
                if let returnedPid, let nonMain = after.first(where: { $0.pid == returnedPid }) {
                    let exe = nonMain.executablePath.map { URL(fileURLWithPath: $0).lastPathComponent } ?? "?"
                    Log.write("launch: \(label) openApplication returned non-main imposter (pid \(returnedPid), \(exe), attempt \(attempt)); retrying")
                } else {
                    Log.write("launch: \(label) exited right after launch (attempt \(attempt)), retrying")
                }
            } catch {
                Log.write("launch: \(label) openApplication attempt \(attempt) failed: \(error.localizedDescription)")
                guard !Task.isCancelled, canRetry() else { return nil }
                let beforeFallback = hooks.snapshot()
                if case .activateExisting(let pid) = planLaunch(instances: beforeFallback, expectedBundleID: bundleID, currentUID: currentUID, excludedCmdSubstrings: excludedCmdSubstrings) {
                    hooks.activate(pid)
                    onConfirmedPid?(pid)
                    Log.write("launch: \(label) already running (pid \(pid)), activated")
                    return pid
                }
                hooks.openFallback(useNewInstance)
                await hooks.sleep(settleNanos)
                guard !Task.isCancelled, canRetry() else { return nil }
                let afterFallback = hooks.snapshot()
                if case .activateExisting(let pid) = planLaunch(instances: afterFallback, expectedBundleID: bundleID, currentUID: currentUID, excludedCmdSubstrings: excludedCmdSubstrings) {
                    onConfirmedPid?(pid)
                    Log.write("launch: \(label) running via open fallback (pid \(pid))")
                    return pid
                }
            }
        }
        if !Task.isCancelled { Log.write("ERROR launch: \(label) did not start after 3 attempts") }
        return nil
    }

    @MainActor
    static func launchLive(
        label: String,
        bundleID: String,
        appURL: URL,
        excludedCmdSubstrings: [String] = [],
        canRetry: @escaping @MainActor () -> Bool = { true },
        onConfirmedPid: (@MainActor (pid_t) -> Void)? = nil
    ) async -> pid_t? {
        let hooks = Hooks(
            snapshot: { snapshotLive(bundleID: bundleID, excludedCmdSubstrings: excludedCmdSubstrings) },
            activate: { pid in NSRunningApplication(processIdentifier: pid)?.activate() },
            reap: { inst in await reapOrphanLive(inst, expectedBundleID: bundleID, label: label) },
            open: { newInstance in
                let cfg = NSWorkspace.OpenConfiguration()
                cfg.activates = true
                cfg.createsNewApplicationInstance = newInstance
                let app = try await NSWorkspace.shared.openApplication(at: appURL, configuration: cfg)
                return app.processIdentifier
            },
            openFallback: { newInstance in
                let args = newInstance ? ["-n", "-a", appURL.path] : ["-a", appURL.path]
                _ = Happ.run("/usr/bin/open", args)
            },
            sleep: { ns in try? await Task.sleep(nanoseconds: ns) }
        )
        return await runLaunchLoop(
            label: label,
            bundleID: bundleID,
            excludedCmdSubstrings: excludedCmdSubstrings,
            canRetry: canRetry,
            onConfirmedPid: onConfirmedPid,
            hooks: hooks
        )
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
              app.activationPolicy != .prohibited,
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
        for _ in 0..<300 {                      // up to 30 seconds (100ms poll)
            if app.isTerminated || running == nil { break }
            try? await Task.sleep(nanoseconds: 100_000_000)
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
            for _ in 0..<40 where running != nil { try? await Task.sleep(nanoseconds: 100_000_000) }
        }
        // Wait for Electron helpers to flush LevelDB and exit (capped at 1.0s), then short 150ms settle.
        for _ in 0..<10 {
            let (rc, _) = Happ.run("/usr/bin/pgrep", ["-f", "Claude\\.app/Contents/Frameworks/Claude Helper"])
            if rc != 0 { break }
            try? await Task.sleep(nanoseconds: 100_000_000)
        }
        try? await Task.sleep(nanoseconds: 150_000_000)
        Log.write("quit: \(running == nil ? "Claude closed" : "Claude still running")")
        return running == nil
    }

    @MainActor private static var launchTask: Task<pid_t?, Never>?

    @MainActor
    @discardableResult
    static func launchAndWait(onConfirmedPid: (@MainActor (pid_t) -> Void)? = nil) async -> pid_t? {
        if fakeClaude { Log.write("launch: (self-test) Claude would be launched"); return nil }
        guard let url = appURL else { Log.write("launch: Claude.app not found"); return nil }
        launchTask?.cancel()
        Log.write("launch: launching Claude (\(url.path))")
        let task = Task { @MainActor in
            await AppLaunch.launchLive(label: "Claude", bundleID: claudeBundleID, appURL: url, onConfirmedPid: onConfirmedPid)
        }
        launchTask = task
        return await task.value
    }

    @MainActor
    static func launch(onConfirmedPid: (@MainActor (pid_t) -> Void)? = nil) {
        if fakeClaude { Log.write("launch: (self-test) Claude would be launched"); return }
        guard let url = appURL else { Log.write("launch: Claude.app not found"); return }
        launchTask?.cancel()
        Log.write("launch: launching Claude (\(url.path))")
        launchTask = Task { @MainActor in
            await AppLaunch.launchLive(label: "Claude", bundleID: claudeBundleID, appURL: url, onConfirmedPid: onConfirmedPid)
        }
    }
}

// MARK: - Controlling T3 Code

enum T3Code {
    struct ActiveRun {
        let threadId: String
        let title: String
        let provider: String
        let status: String
    }

    /// Selftest hooks (used only when `fakeClaude == true`) so codex-selftest exercises the real quit -> promote -> launch flow.
    struct TestController {
        var isRunning: () -> Bool
        var quit: () async -> Bool
        var launch: () async -> pid_t?
    }
    nonisolated(unsafe) static var testController: TestController?

    static let excludedCmdSubstrings = ["apps/server/dist/bin.mjs", "acp-mcp-"]

    @MainActor private static var launchTask: Task<pid_t?, Never>?

    static var isRunningNow: Bool {
        if fakeClaude { return testController?.isRunning() ?? false }
        return realRunning != nil
    }

    static var running: NSRunningApplication? {
        if fakeClaude { return nil }
        return realRunning
    }
    static var realRunning: NSRunningApplication? {
        NSRunningApplication.runningApplications(withBundleIdentifier: t3BundleID).first { !$0.isTerminated && isMainProcess($0) }
    }

    static func isMainProcess(_ app: NSRunningApplication) -> Bool {
        guard app.bundleIdentifier == t3BundleID,
              app.activationPolicy != .prohibited,
              let exe = app.executableURL,
              let main = app.bundleURL.flatMap({ Bundle(url: $0)?.executableURL }) else { return false }
        guard exe.resolvingSymlinksInPath().path == main.resolvingSymlinksInPath().path else { return false }
        if let cmd = AppLaunch.commandLine(app.processIdentifier),
           excludedCmdSubstrings.contains(where: { cmd.contains($0) }) {
            return false
        }
        return true
    }

    /// True if T3 Code's own embedded backend server (`apps/server/dist/bin.mjs`, excluding agent `acp-mcp-*` helpers) is running.
    static func isOwnServerRunning() -> Bool {
        let (rc, out) = Happ.run("/bin/ps", ["-axo", "command="])
        guard rc == 0 else { return false }
        let mainExe = appURL.flatMap { Bundle(url: $0)?.executableURL }?.resolvingSymlinksInPath().path
        for line in out.split(separator: "\n") {
            if line.contains("acp-mcp-") { continue }
            if line.contains("apps/server/dist/bin.mjs") { return true }
            if let mainExe, line.contains(mainExe) { return true }
            if line.contains("T3 Code (Beta).app/Contents/MacOS/T3 Code") { return true }
        }
        return false
    }

    static var appURL: URL? {
        NSWorkspace.shared.urlForApplication(withBundleIdentifier: t3BundleID)
            ?? (fm.fileExists(atPath: "/Applications/T3 Code (Beta).app") ? URL(fileURLWithPath: "/Applications/T3 Code (Beta).app") : nil)
    }

    /// Reads T3 Code's SQLite database read-only to see if any threads are currently generating or queued.
    static func activeRuns() -> [ActiveRun] {
        guard fm.fileExists(atPath: t3StateDB.path) else { return [] }
        var db: OpaquePointer?
        let encoded = t3StateDB.path.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? t3StateDB.path
        let uri = "file:\(encoded)?mode=ro"
        guard sqlite3_open_v2(uri, &db, SQLITE_OPEN_READONLY | SQLITE_OPEN_URI, nil) == SQLITE_OK, let db else {
            if db != nil { sqlite3_close(db) }
            return []
        }
        defer { sqlite3_close(db) }
        sqlite3_busy_timeout(db, 1000)

        var seen = Set<String>()
        var result: [ActiveRun] = []

        func runQuery(_ sql: String) {
            var stmt: OpaquePointer?
            guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK, let stmt else { return }
            defer { sqlite3_finalize(stmt) }
            while sqlite3_step(stmt) == SQLITE_ROW {
                func col(_ i: Int32) -> String {
                    sqlite3_column_text(stmt, i).map { String(cString: $0) } ?? ""
                }
                let tid = col(0)
                guard !tid.isEmpty, !seen.contains(tid) else { continue }
                seen.insert(tid)
                result.append(ActiveRun(threadId: tid, title: col(3), provider: col(1), status: col(2)))
            }
        }

        runQuery("""
            SELECT r.thread_id, COALESCE(r.provider, ''), r.status, COALESCE(t.title, '')
            FROM orchestration_v2_projection_runs r
            LEFT JOIN orchestration_v2_projection_threads t ON t.thread_id = r.thread_id
            WHERE r.status IN ('running', 'starting', 'preparing', 'queued', 'waiting')
              AND COALESCE(t.deleted_at, '') = '' AND COALESCE(t.archived_at, '') = ''
            """)
        runQuery("""
            SELECT s.thread_id, COALESCE(s.provider_name, ''), COALESCE(s.status, ''), COALESCE(t.title, '')
            FROM projection_thread_sessions s
            LEFT JOIN projection_threads t ON t.thread_id = s.thread_id
            WHERE (s.active_turn_id IS NOT NULL OR s.status IN ('running', 'starting', 'preparing', 'working'))
              AND COALESCE(t.deleted_at, '') = '' AND COALESCE(t.archived_at, '') = ''
            """)
        return result
    }

    @MainActor
    static func quit() async -> Bool {
        launchTask?.cancel()
        launchTask = nil
        if fakeClaude, let tc = testController { return await tc.quit() }
        if let app = running {
            Log.write("quit: quitting T3 Code (pid \(app.processIdentifier))")
            app.terminate()
            for _ in 0..<150 {                      // up to 15 seconds (100ms poll)
                if app.isTerminated || running == nil { break }
                try? await Task.sleep(nanoseconds: 100_000_000)
            }
            if let still = running {
                let alert = NSAlert()
                alert.messageText = L("T3 Code did not quit", "T3 Code не закрылся")
                alert.informativeText = L("It may be finishing a turn or waiting for confirmation. Force quit it?",
                                          "Возможно, он завершает задачу или ждёт подтверждения. Закрыть принудительно?")
                alert.addButton(withTitle: L("Force quit", "Закрыть принудительно"))
                alert.addButton(withTitle: L("Cancel", "Отмена"))
                NSApp.activate(ignoringOtherApps: true)
                guard alert.runModal() == .alertFirstButtonReturn else { Log.write("quit: T3 Code cancelled"); return false }
                Log.write("quit: T3 Code forced")
                still.forceTerminate()
                for _ in 0..<40 where running != nil { try? await Task.sleep(nanoseconds: 100_000_000) }
            }
        } else {
            Log.write("quit: T3 Code is not running")
        }
        // Wait briefly for T3 Code's own backend server (ignoring agent acp-mcp-* processes) to exit.
        if !fakeClaude {
            for _ in 0..<25 {
                if !isOwnServerRunning() { break }
                try? await Task.sleep(nanoseconds: 100_000_000)
            }
            if isOwnServerRunning() {
                Log.write("quit: WARNING T3 Code backend server still running after 2.5s")
            }
        }
        Log.write("quit: \(running == nil ? "T3 Code closed" : "T3 Code still running")")
        return running == nil
    }

    @MainActor
    @discardableResult
    static func launchAndWait(canRetry: @escaping @MainActor () -> Bool = { true }, onConfirmedPid: (@MainActor (pid_t) -> Void)? = nil) async -> pid_t? {
        if fakeClaude {
            if let tc = testController {
                let pid = await tc.launch()
                if let pid { onConfirmedPid?(pid) }
                return pid
            }
            Log.write("launch: (self-test) T3 Code would be launched")
            return nil
        }
        guard let url = appURL else { Log.write("launch: T3 Code.app not found"); return nil }
        launchTask?.cancel()
        Log.write("launch: launching T3 Code (\(url.path))")
        let task = Task { @MainActor in
            await AppLaunch.launchLive(
                label: "T3 Code",
                bundleID: t3BundleID,
                appURL: url,
                excludedCmdSubstrings: excludedCmdSubstrings,
                canRetry: canRetry,
                onConfirmedPid: onConfirmedPid
            )
        }
        launchTask = task
        return await task.value
    }

    @MainActor
    static func launch(canRetry: @escaping @MainActor () -> Bool = { true }, onConfirmedPid: (@MainActor (pid_t) -> Void)? = nil) {
        if fakeClaude {
            if let tc = testController {
                launchTask?.cancel()
                launchTask = Task { @MainActor in
                    let pid = await tc.launch()
                    if let pid { onConfirmedPid?(pid) }
                    return pid
                }
                return
            }
            Log.write("launch: (self-test) T3 Code would be launched")
            return
        }
        guard let url = appURL else { Log.write("launch: T3 Code.app not found"); return }
        launchTask?.cancel()
        Log.write("launch: launching T3 Code (\(url.path))")
        launchTask = Task { @MainActor in
            await AppLaunch.launchLive(
                label: "T3 Code",
                bundleID: t3BundleID,
                appURL: url,
                excludedCmdSubstrings: excludedCmdSubstrings,
                canRetry: canRetry,
                onConfirmedPid: onConfirmedPid
            )
        }
    }
}

// MARK: - VPN Happ
// Happ (com.happ.www) understands the links happ://connect, happ://disconnect, happ://close.
// Connected = the default route goes through utun and the xray core is running.

enum Happ {
    static let bundleID = "com.happ.www"

    static var installed: Bool { NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundleID) != nil }
    static var app: NSRunningApplication? {
        let apps = NSRunningApplication.runningApplications(withBundleIdentifier: bundleID).filter { !$0.isTerminated }
        if let main = apps.first(where: { app in
            guard let exe = app.executableURL?.resolvingSymlinksInPath().path,
                  let expected = app.bundleURL.flatMap({ Bundle(url: $0)?.executableURL })?.resolvingSymlinksInPath().path else { return false }
            return exe == expected
        }) {
            return main
        }
        return apps.first { $0.bundleURL.flatMap({ Bundle(url: $0)?.executableURL }) == nil }
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
    static func connect(prefetchedDirectIP: IPInfo? = nil) async -> Bool {
        await ensureService()
        Log.write("happ: connecting. Now: \(state())")
        if !isConnected() {
            let directInfo: IPInfo?
            if let prefetchedDirectIP { directInfo = prefetchedDirectIP }
            else { directInfo = await publicIP() }
            if let d = directInfo { directIP = d.ip; Log.write("happ: direct IP before connecting: \(d.ip) (\(d.place))") }
            else { Log.write("happ: could not get the direct IP (no internet?)") }
            if app == nil, let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundleID) {
                Log.write("happ: launching Happ.app before happ://connect")
                let cfg = NSWorkspace.OpenConfiguration(); cfg.activates = false
                _ = try? await NSWorkspace.shared.openApplication(at: url, configuration: cfg)
                for _ in 0..<30 where app == nil { await sleep(0.1) }
                await sleep(0.4)
            }
            for attempt in 1...3 {
                send("connect")
                let maxTicks = attempt == 1 ? 45 : 80   // ~6.7s first attempt, ~12s retry
                for _ in 0..<maxTicks where !isConnected() { await sleep(0.15) }
                if isConnected() { break }
                Log.write("happ: attempt \(attempt) did not bring the tunnel up")
            }
        }
        guard isConnected() else { Log.write("happ: ERROR, did not connect. \(state())"); return false }
        // Run api.anthropic.com reachability probe and external IP check in parallel once tunnel is up.
        async let probeTask: Bool = {
            for i in 1...5 {
                if await probe() { Log.write("happ: internet through the tunnel works (check \(i)). \(state())"); return true }
                await sleep(1.0)
            }
            return false
        }()
        async let ipTask = publicIP()
        let (online, vpnInfo) = await (probeTask, ipTask)
        guard online else { Log.write("happ: ERROR, the tunnel is up but api.anthropic.com does not answer. \(state())"); return false }
        let v: IPInfo?
        if let vpnInfo { v = vpnInfo } else { v = await publicIP() }
        guard let v else { Log.write("happ: ERROR, could not get the external IP through the tunnel"); return false }
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
            for _ in 0..<60 where isConnected() { await sleep(0.15) }        // up to 9 s
        }
        if app != nil {
            send("close")
            for _ in 0..<30 where app != nil { await sleep(0.15) }           // up to 4.5 s
        }
        if let a = app { Log.write("happ: did not quit on happ://close, terminating"); a.terminate()
            for _ in 0..<30 where app != nil { await sleep(0.15) } }
        if let a = app { a.forceTerminate(); await sleep(0.5) }
        guard app == nil, !isConnected() else { Log.write("happ: ERROR, Happ did not quit or the tunnel is still up. \(state())"); return false }
        // Wait until the network is direct again and check that the external IP is no longer the VPN one.
        var ip: IPInfo?
        for _ in 1...3 { ip = await publicIP(); if ip != nil { break }; await sleep(1.0) }
        guard let ip else { Log.write("happ: closed, no tunnel, but could not get the external IP (network still coming up?). \(state())"); return true }
        if let v = vpnIP, v == ip.ip { Log.write("happ: ERROR, after quitting the IP is still the VPN one (\(ip.ip))"); return false }
        directIP = ip.ip
        Log.write("happ: ALL OK. Happ fully quit, no tunnel, the IP is direct again: \(ip.ip) (\(ip.place))")
        return true
    }
}

// MARK: - Usage collector refresh after account switch

enum UsageCollector {
    static let switchStartedNotification = Notification.Name("com.lemelson.claudeswitcher.accountDidSwitch")
    static let switchFinishedNotification = Notification.Name("com.lemelson.claudeswitcher.usageRefreshDidFinish")
    static let stateFile = storeDir.appendingPathComponent("usage/state.json")
    static let launchAgentPlist = home.appendingPathComponent("Library/LaunchAgents/com.lemelson.claude-usage.plist")

    private static func isValidCollector(python: String, tick: String) -> Bool {
        tick.hasSuffix("/usage/tick.py")
            && fm.fileExists(atPath: tick)
            && fm.isExecutableFile(atPath: python)
    }

    /// Reads the collector python and `usage/tick.py` paths from `state.json` (same contract CodexBar uses),
    /// falling back to the installed LaunchAgent plist if `state.json` is absent or temporarily unreadable.
    static func resolve() -> (python: String, tick: String)? {
        if let data = try? Data(contentsOf: stateFile),
           let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let collector = root["collector"] as? [String: Any],
           let python = collector["python"] as? String,
           let tick = collector["tick"] as? String,
           isValidCollector(python: python, tick: tick) {
            return (python, tick)
        }
        if let data = try? Data(contentsOf: launchAgentPlist),
           let plist = try? PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any],
           let args = plist["ProgramArguments"] as? [String],
           args.count >= 2,
           isValidCollector(python: args[0], tick: args[1]) {
            return (args[0], args[1])
        }
        return nil
    }

    /// Fires a non-blocking `tick.py --switch` pass right after an account switch finishes and notifies CodexBar
    /// when the refresh begins and ends so both the menu bar and open menu update immediately.
    static func triggerAfterSwitch(account: Account?) {
        triggerAfterSwitch(uuid: account?.accountUuid ?? "", label: account?.name ?? "unknown", provider: "claude")
    }

    /// The `tick.py` arguments of a switch pass. Claude: `--account <uuid>` (the account the pass must find
    /// active). Codex: `--provider codex` (the Claude side did not change: only the Codex block and spend24h).
    static func tickArguments(tick: String, uuid: String, provider: String) -> [String] {
        if provider == "codex" { return [tick, "--switch", "--provider", "codex"] }
        return uuid.isEmpty ? [tick, "--switch"] : [tick, "--switch", "--account", uuid]
    }

    /// The last switch refresh asked for (the self-test reads it; the self-test itself starts no collector).
    nonisolated(unsafe) static var lastRequest: (uuid: String, provider: String)?

    /// `uuid`: the newly active account, in both notifications: a Claude account uuid, or for Codex the
    /// account's state.json uuid (CodexAccount.stateUuid).
    static func triggerAfterSwitch(uuid: String, label: String, provider: String) {
        lastRequest = (uuid, provider)
        guard !fakeClaude else { return }
        let token = UUID().uuidString
        let shortToken = String(token.prefix(8))

        var startedProcess: Process?
        // Gate terminationHandler so switchFinishedNotification can never be posted before switchStartedNotification.
        let orderLock = NSLock()
        var startedPosted = false
        var deferredFinish: Int?

        func postFinished(_ rc: Int) {
            DistributedNotificationCenter.default().postNotificationName(
                switchFinishedNotification,
                object: nil,
                userInfo: ["token": token, "uuid": uuid, "exitCode": rc, "provider": provider],
                deliverImmediately: true
            )
        }

        if let collector = resolve() {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: collector.python)
            p.arguments = tickArguments(tick: collector.tick, uuid: uuid, provider: provider)
            if provider == "codex" {
                var env = ProcessInfo.processInfo.environment
                env["CODEX_HOME"] = CodexAccounts.codexHome.path
                p.environment = env
            }
            p.standardOutput = FileHandle.nullDevice
            p.standardError = FileHandle.nullDevice

            let watchdog = DispatchWorkItem {
                if p.isRunning {
                    Log.write("usage refresh: timed out after 65s, terminating (token=\(shortToken))")
                    p.terminate()
                }
            }
            p.terminationHandler = { proc in
                watchdog.cancel()
                let rc = Int(proc.terminationStatus)
                Log.write("usage refresh: finished for “\(label)” (rc=\(rc), token=\(shortToken))")
                orderLock.lock()
                let canPostNow = startedPosted
                if !canPostNow { deferredFinish = rc }
                orderLock.unlock()
                if canPostNow { postFinished(rc) }
            }
            do {
                try p.run()
                startedProcess = p
                DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 65, execute: watchdog)
                Log.write("usage refresh: started tick.py \(p.arguments!.dropFirst().joined(separator: " ")) for “\(label)” (token=\(shortToken))")
            } catch {
                p.terminationHandler = nil
                Log.write("usage refresh: failed to launch collector for “\(label)”: \(error.localizedDescription)")
            }
        } else {
            Log.write("usage refresh: collector not found, notifying CodexBar only (token=\(shortToken))")
        }

        DistributedNotificationCenter.default().postNotificationName(
            switchStartedNotification,
            object: nil,
            userInfo: [
                "token": token,
                "uuid": uuid,
                "collectorStarted": startedProcess != nil,
                "provider": provider,
            ],
            deliverImmediately: true
        )
        orderLock.lock()
        startedPosted = true
        let delayedRc = deferredFinish
        deferredFinish = nil
        orderLock.unlock()
        if let rc = delayedRc { postFinished(rc) }
    }
}

// MARK: - Codex accounts
// The ChatGPT accounts Codex can use on this Mac: the live system account in ~/.codex/auth.json and the
// accounts CodexBar keeps in managed-codex-accounts.json. Everything here only reads those files (never a
// token kept, logged or refreshed). Switching is CodexBar's own `CodexBarCLI codex-accounts promote`, which
// first preserves the live account as a managed one and then swaps ~/.codex/auth.json.
// The same discovery rules as usage/codex_accounts.py (CODEX-STATE.md, "Accounts list").

struct CodexAccount {
    /// Normalized (lowercase) ChatGPT account id; nil when only the e-mail is known.
    var accountId: String?
    var email: String?
    /// CodexBar's managed-account UUID (exact case, as `promote` wants it), if CodexBar keeps this account.
    var managedId: String?
    var rawPlan: String?
    var isActive = false
    var displayName = ""
    var sources: [String] = []

    var key: String? { accountId ?? email.map { "email:\($0)" } }
    /// The `uuid` of this account's block in state.json (usage/codex_accounts.block_uuid): the account id, else
    /// "email-hash:" + the first 12 hex digits of the e-mail's SHA-256. Never empty.
    var stateUuid: String { accountId ?? email.map(CodexAccounts.emailUuid) ?? "unknown" }
    /// What `promote` gets: the exact managed UUID, else the e-mail.
    var promoteTarget: String? { managedId ?? email }
    var planName: String? { CodexAccounts.planName(rawPlan) }
}

enum CodexAccounts {
    static let codexHome: URL = {
        let env = ProcessInfo.processInfo.environment
        if let p = env["CLAUDE_SWITCHER_CODEX_HOME"] ?? env["CODEX_HOME"] { return URL(fileURLWithPath: p) }
        return home.appendingPathComponent(".codex")
    }()
    static let codexBarDir: URL = {
        if let p = ProcessInfo.processInfo.environment["CLAUDE_SWITCHER_CODEXBAR_DIR"] { return URL(fileURLWithPath: p) }
        return home.appendingPathComponent("Library/Application Support/CodexBar")
    }()
    static let authFile = codexHome.appendingPathComponent("auth.json")
    static let managedFile = codexBarDir.appendingPathComponent("managed-codex-accounts.json")
    static let managedHomes = codexBarDir.appendingPathComponent("managed-codex-homes")
    /// The owner's display names (Claude and Codex accounts), kept next to the usage data.
    static let namesFile = storeDir.appendingPathComponent("usage/account-names.json")
    static let cliPath = ProcessInfo.processInfo.environment["CLAUDE_SWITCHER_CODEXBAR_CLI"]
        ?? "/Applications/CodexBar.app/Contents/Helpers/CodexBarCLI"

    static func emailUuid(_ email: String) -> String {
        "email-hash:" + SHA256.hash(data: Data(email.utf8)).map { String(format: "%02x", $0) }.joined().prefix(12)
    }

    static func norm(_ s: Any?) -> String? {
        guard let s = s as? String else { return nil }
        let t = s.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        return t.isEmpty ? nil : t
    }

    static func planName(_ raw: String?) -> String? {
        guard let raw = norm(raw) else { return nil }
        switch raw {
        case "plus": return "Plus"
        case "prolite": return "Pro 5x"
        case "pro": return "Pro"
        case "team", "business": return "Business"
        case "enterprise": return "Enterprise"
        case "edu": return "Edu"
        case "free": return "Free"
        case "go": return "Go"
        default: return raw.prefix(1).uppercased() + raw.dropFirst()
        }
    }

    /// The payload of a JWT (no signature check: only the e-mail, the account id and the plan are read).
    static func jwtClaims(_ token: String) -> [String: Any] {
        let parts = token.split(separator: ".")
        guard parts.count >= 2 else { return [:] }
        var b64 = String(parts[1]).replacingOccurrences(of: "-", with: "+").replacingOccurrences(of: "_", with: "/")
        b64 += String(repeating: "=", count: (4 - b64.count % 4) % 4)
        guard let data = Data(base64Encoded: b64),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return [:] }
        return obj
    }

    /// (account id, e-mail, raw plan) of a Codex auth.json, read-only.
    static func identity(authFile: URL) -> (id: String?, email: String?, plan: String?)? {
        guard let data = try? Data(contentsOf: authFile),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return nil }
        let tokens = obj["tokens"] as? [String: Any] ?? [:]
        let claims = (tokens["id_token"] as? String).map(jwtClaims) ?? [:]
        let auth = claims["https://api.openai.com/auth"] as? [String: Any] ?? [:]
        let id = norm(tokens["account_id"]) ?? norm(auth["chatgpt_account_id"])
        let email = norm(claims["email"])
        guard id != nil || email != nil else { return nil }
        return (id, email, norm(auth["chatgpt_plan_type"]))
    }

    /// Top-level keys of a flat JSON object in file order (JSONSerialization loses the order).
    static func orderedKeys(_ text: String) -> [String] {
        var keys: [String] = []
        var depth = 0, inString = false, escaped = false
        var current = "", last: String?
        for ch in text {
            if inString {
                if escaped { current.append(ch); escaped = false }
                else if ch == "\\" { escaped = true }
                else if ch == "\"" { inString = false; last = current }
                else { current.append(ch) }
                continue
            }
            switch ch {
            case "\"": inString = true; current = ""
            case "{", "[": depth += 1; last = nil
            case "}", "]": depth -= 1; last = nil
            case ":": if depth == 1, let k = last { keys.append(k) }; last = nil
            case ",": last = nil
            default: break
            }
        }
        return keys
    }

    /// The owner's names: (lowercased key -> name, lowercased keys in file order).
    static func displayNames() -> ([String: String], [String]) {
        guard let data = try? Data(contentsOf: namesFile),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return ([:], []) }
        var names: [String: String] = [:]
        for (k, v) in obj {
            if let v = (v as? String)?.trimmingCharacters(in: .whitespacesAndNewlines), !v.isEmpty { names[k.lowercased()] = v }
        }
        return (names, orderedKeys(String(decoding: data, as: UTF8.self)).map { $0.lowercased() })
    }

    /// Every Codex account on this Mac, one per lowercase workspace account id (else e-mail), in the order of the
    /// ids in account-names.json, then by e-mail. The account of ~/.codex/auth.json is the active one.
    /// Two passes, as usage/codex_accounts.py: first every record with an id; then each e-mail-only record joins
    /// the one entry with that e-mail, or stays its own entry when none or several (two workspaces of one login)
    /// have it. The active mark follows the live record wherever it lands.
    static func discover() -> [CodexAccount] {
        typealias Raw = (id: String?, email: String?, source: String, managedId: String?, plan: String?)
        var raw: [Raw] = []
        if let live = identity(authFile: authFile) { raw.append((live.id, live.email, "live", nil, live.plan)) }
        if let data = try? Data(contentsOf: managedFile),
           let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let list = obj["accounts"] as? [[String: Any]] {
            let homesRoot = managedHomes.resolvingSymlinksInPath().path + "/"
            for r in list {
                // CodexBar's effectiveWorkspaceAccountID: the workspace first, then the provider account
                var id = norm(r["workspaceAccountID"]) ?? norm(r["providerAccountID"])
                var email = norm(r["email"])
                var plan: String?
                // Only a home inside CodexBar's own folder is looked into (its auth.json, for the plan claim).
                if let h = r["managedHomePath"] as? String,
                   URL(fileURLWithPath: h).resolvingSymlinksInPath().path.hasPrefix(homesRoot),
                   let ident = identity(authFile: URL(fileURLWithPath: h).appendingPathComponent("auth.json")) {
                    id = id ?? ident.id; email = email ?? ident.email; plan = ident.plan
                }
                raw.append((id, email, "managed", r["id"] as? String, plan))
            }
        }
        var found: [CodexAccount] = []
        func merge(_ j: Int, _ r: Raw) {
            if found[j].email == nil { found[j].email = r.email }
            if found[j].managedId == nil { found[j].managedId = r.managedId }
            if found[j].rawPlan == nil { found[j].rawPlan = r.plan }
            if !found[j].sources.contains(r.source) { found[j].sources.append(r.source) }
        }
        func new(_ id: String?, _ email: String?) -> Int { found.append(CodexAccount(accountId: id, email: email)); return found.count - 1 }
        for r in raw where r.id != nil {
            merge(found.firstIndex { $0.accountId == r.id } ?? new(r.id, r.email), r)
        }
        for r in raw where r.id == nil {
            guard let email = r.email else { continue }
            let withId = found.indices.filter { found[$0].accountId != nil && found[$0].email == email }
            let j = withId.count == 1 ? withId[0]
                : (found.firstIndex { $0.accountId == nil && $0.email == email } ?? new(nil, email))
            merge(j, r)
        }
        let (names, order) = displayNames()
        func rank(_ a: CodexAccount) -> Int {
            a.accountId.flatMap { order.firstIndex(of: $0) } ?? a.email.flatMap { order.firstIndex(of: $0) } ?? order.count
        }
        for i in found.indices {
            found[i].isActive = found[i].sources.contains("live")
            found[i].displayName = found[i].accountId.flatMap { names[$0] } ?? found[i].email.flatMap { names[$0] }
                ?? found[i].email ?? found[i].accountId ?? "?"
        }
        return found.sorted { (rank($0), $0.email ?? "", $0.accountId ?? "") < (rank($1), $1.email ?? "", $1.accountId ?? "") }
    }

    struct PromoteResult {
        let exitCode: Int32
        let stdout: String
        let stderr: String
        var ok: Bool { exitCode == 0 }
    }

    /// Runs `CodexBarCLI codex-accounts promote <target> --json` off the main thread (60 s at most).
    static func promote(_ target: String) async -> PromoteResult {
        await withCheckedContinuation { cont in
            DispatchQueue.global(qos: .userInitiated).async {
                let p = Process()
                p.executableURL = URL(fileURLWithPath: cliPath)
                p.arguments = ["codex-accounts", "promote", target, "--json"]
                // CodexBarCLI swaps the auth.json of the same folder this app reads (CLAUDE_SWITCHER_CODEX_HOME)
                var env = ProcessInfo.processInfo.environment
                env["CODEX_HOME"] = codexHome.path
                p.environment = env
                let out = Pipe(), err = Pipe()
                p.standardOutput = out; p.standardError = err
                p.standardInput = FileHandle.nullDevice
                do { try p.run() } catch {
                    cont.resume(returning: PromoteResult(exitCode: -1, stdout: "", stderr: error.localizedDescription)); return
                }
                let watchdog = DispatchWorkItem { if p.isRunning { p.terminate() } }
                DispatchQueue.global().asyncAfter(deadline: .now() + 60, execute: watchdog)
                // stderr on its own queue so a full pipe never blocks the other one
                var errData = Data()
                let group = DispatchGroup()
                group.enter()
                DispatchQueue.global().async { errData = err.fileHandleForReading.readDataToEndOfFile(); group.leave() }
                let outData = out.fileHandleForReading.readDataToEndOfFile()
                group.wait()
                p.waitUntilExit()
                watchdog.cancel()
                cont.resume(returning: PromoteResult(exitCode: p.terminationStatus,
                                                     stdout: String(decoding: outData, as: UTF8.self),
                                                     stderr: String(decoding: errData, as: UTF8.self)))
            }
        }
    }
}

// MARK: - Menu bar app

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    let profiles = Profiles()
    var statusItem: NSStatusItem!
    var busy = false
    var suppressLaunchPromptUntil = Date.distantPast
    var suppressT3LaunchPromptUntil = Date.distantPast
    var managedClaudeLaunchPids = Set<pid_t>()
    var managedT3LaunchPids = Set<pid_t>()
    var watcher: ClaudeLogWatcher!
    /// After a switch we wait to see whether the server accepts the login we put in place for this account.
    var expecting: (uuid: String, until: Date)?
    var autosaveWork: DispatchWorkItem?
    var declinedUuids = Set<String>()
    var promptOpen = false
    var lastAlert: String?
    /// The text of the last alert (self-test only, like lastAlert).
    var lastAlertText: String?

    func applicationDidFinishLaunching(_ n: Notification) {
        _ = profiles.syncLiveCliToMatchingProfile()
        // usage/core.py (switch_timeline) parses this line: do not reword the start or "in Claude now: <uuid>".
        // Builds before November 2026 wrote it in Russian ("в Claude сейчас"); core.py accepts both forms.
        Log.write("=== Claude Switcher started. Accounts: \(profiles.store.accounts.map { "\($0.name)=\($0.accountUuid ?? "-")\($0.needsRelogin == true ? "(needs sign-in)" : "")\(profiles.hasCliLogin($0) ? "[cli]" : "")" }.joined(separator: ", ")); in Claude now: \(profiles.currentUuid() ?? "nobody"); Claude \(Claude.running == nil ? "closed" : "open"); in CLI now: \(ClaudeCLI.currentUuid() ?? "nobody"); T3Code \(T3Code.running == nil ? "closed" : "open")")
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
        _ = profiles.syncLiveCliToMatchingProfile()
        menu.removeAllItems()
        let curUuid = profiles.currentUuid()
        let curCliUuid = ClaudeCLI.currentUuid()
        let current = curUuid.flatMap { profiles.account(withUuid: $0) }

        let header = NSMenuItem(title: L("Claude accounts", "Аккаунты Claude"), action: nil, keyEquivalent: "")
        header.isEnabled = false
        menu.addItem(header)

        if profiles.store.accounts.isEmpty {
            let empty = NSMenuItem(title: L("No saved accounts yet", "Пока нет сохранённых аккаунтов"), action: nil, keyEquivalent: "")
            empty.isEnabled = false; menu.addItem(empty)
        }
        for (i, a) in profiles.store.accounts.enumerated() {
            let hasCli = profiles.hasCliLogin(a)
            let cliNote = hasCli ? "" : L(" · CLI sign-in needed", " · нужен вход CLI")
            let title = a.name + (a.useHapp == true ? "  · VPN" : "") + (a.needsRelogin == true ? L(" — sign in again", " — нужно войти заново") : "") + cliNote
            let item = NSMenuItem(title: title, action: #selector(pickAccount(_:)), keyEquivalent: i < 9 ? "\(i + 1)" : "")
            item.target = self
            item.representedObject = a.id
            item.state = (a.id == current?.id) ? .on : .off
            item.isEnabled = !busy
            menu.addItem(item)
        }
        if let curUuid, curCliUuid != curUuid {
            let cliName = curCliUuid.flatMap { profiles.account(withUuid: $0)?.name } ?? ClaudeCLI.currentEmail() ?? L("not signed in", "нет входа")
            let syncNote = NSMenuItem(title: L("T3 Code / CLI now: \(cliName)", "В T3 Code / CLI сейчас: \(cliName)"), action: nil, keyEquivalent: "")
            syncNote.isEnabled = false
            menu.addItem(syncNote)
        }
        menu.addItem(.separator())

        // Codex accounts (hidden when Codex has no account on this Mac)
        let codex = CodexAccounts.discover()
        if !codex.isEmpty {
            let codexHeader = NSMenuItem(title: L("Codex accounts", "Аккаунты Codex"), action: nil, keyEquivalent: "")
            codexHeader.isEnabled = false
            menu.addItem(codexHeader)
            for c in codex {
                let title = c.displayName + (c.planName.map { "  · \($0)" } ?? "")
                let item = NSMenuItem(title: title, action: #selector(pickCodexAccount(_:)), keyEquivalent: "")
                item.target = self
                item.representedObject = c.key
                item.state = c.isActive ? .on : .off
                item.toolTip = c.email
                item.isEnabled = !busy && (c.isActive || c.promoteTarget != nil)
                menu.addItem(item)
            }
            menu.addItem(.separator())
        }

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
                let cliTitle = profiles.hasCliLogin(a)
                    ? L("Re-sign in CLI / T3 Code…", "Обновить вход CLI / T3 Code…")
                    : L("Sign in CLI / T3 Code…", "Войти в CLI / T3 Code…")
                let c = NSMenuItem(title: cliTitle, action: #selector(signInCliForAccount(_:)), keyEquivalent: "")
                c.target = self; c.representedObject = a.id; c.isEnabled = !busy; s.addItem(c)
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
        let askT3 = add(menu, L("Confirm before restarting busy T3 Code", "Спрашивать перед перезапуском занятого T3 Code"), #selector(toggleAskBusyT3))
        askT3.state = profiles.store.askBeforeRestartingBusyT3 ? .on : .off
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

    @objc func pickCodexAccount(_ sender: NSMenuItem) {
        guard let key = sender.representedObject as? String,
              let c = CodexAccounts.discover().first(where: { $0.key == key }) else { return }
        guard !c.isActive else { Log.write("codex switch: “\(c.displayName)” is already the active Codex account"); return }
        Task { await switchCodexTo(c) }
    }

    /// Makes `target` the system Codex account (~/.codex/auth.json) with CodexBar's `promote`. T3 Code runs
    /// Codex with the account it started with, so it is restarted around the swap (after the busy check).
    func switchCodexTo(_ target: CodexAccount) async {
        guard !busy else { Log.write("codex switch to “\(target.displayName)” rejected: another switch is in progress"); return }
        guard let arg = target.promoteTarget else {
            alert(L("Cannot switch Codex", "Не удалось переключить Codex"),
                  L("“\(target.displayName)” is not one of CodexBar's saved accounts. Add it in CodexBar first.",
                    "«\(target.displayName)» нет среди сохранённых аккаунтов CodexBar. Сначала добавьте его в CodexBar."))
            return
        }
        guard fm.isExecutableFile(atPath: CodexAccounts.cliPath) else {
            Log.write("ERROR codex switch: CodexBarCLI not found at \(CodexAccounts.cliPath)")
            alert(L("CodexBar not found", "CodexBar не найден"),
                  L("Switching Codex accounts needs CodexBar in /Applications.", "Для смены аккаунта Codex нужен CodexBar в /Applications."))
            return
        }
        let beforeIdent = CodexAccounts.identity(authFile: CodexAccounts.authFile)
        let before = beforeIdent?.id
        let discovered = CodexAccounts.discover()
        // ids of the other listed accounts: an e-mail-only target must not "land" on one of them
        let otherIds = Set(discovered.compactMap(\.accountId).filter { $0 != target.accountId })
        let emailUnique = target.email.map { e in discovered.filter { $0.email == e }.count <= 1 } ?? false
        let t3WasOpen = T3Code.isRunningNow
        Log.write("codex switch: “\(target.displayName)” (\(target.accountId ?? "-"), \(target.managedId != nil ? "managed id" : "by e-mail")), Codex now \(before ?? beforeIdent?.email.map { _ in "an e-mail-only account" } ?? "nobody"), T3Code \(t3WasOpen ? "open" : "closed")")
        // Taken before the confirmation: its dialog runs the event loop, and a second switch must not start there.
        busy = true; defer { busy = false }
        if t3WasOpen {
            guard confirmBusyT3RestartIfNeeded() else { return }
            guard await T3Code.quit() else { Log.write("codex switch aborted: T3 Code did not quit"); return }
        }
        let r = await CodexAccounts.promote(arg)
        let afterIdent = CodexAccounts.identity(authFile: CodexAccounts.authFile)
        let after = afterIdent?.id
        let sameEmail = target.email != nil && afterIdent?.email == target.email
        let landed: Bool
        if let want = target.accountId {
            // by id; an auth.json that names no id counts by its e-mail only when that e-mail is unique
            landed = after == want || (after == nil && sameEmail && emailUnique)
        } else {
            landed = sameEmail && (after == nil || !otherIds.contains(after!))
        }
        func clip(_ s: String) -> String {
            let t = s.trimmingCharacters(in: .whitespacesAndNewlines).replacingOccurrences(of: "\n", with: " ")
            return t.count > 300 ? String(t.prefix(300)) + "…" : t
        }
        Log.write("codex switch: promote rc=\(r.exitCode)\(r.stderr.isEmpty ? "" : ", stderr: \(clip(r.stderr))"), Codex now \(after ?? (afterIdent == nil ? "nobody" : "an e-mail-only account")), landed=\(landed)")
        var t3LaunchedPid: pid_t?
        if t3WasOpen {
            // relaunched whether or not the promote worked: T3 Code was closed for this switch only
            suppressT3LaunchPromptUntil = Date().addingTimeInterval(30)
            t3LaunchedPid = await T3Code.launchAndWait(onConfirmedPid: { [weak self] pid in
                self?.managedT3LaunchPids.insert(pid)
            })
        }
        let t3LaunchFailed = t3WasOpen && !fakeClaude && t3LaunchedPid == nil
        guard r.ok, landed else {
            // what auth.json holds now, read again: the alert names it instead of assuming the previous account
            let now = CodexAccounts.identity(authFile: CodexAccounts.authFile)
            let nowAccount = CodexAccounts.discover().first { $0.isActive }
            let unchanged = now?.id == beforeIdent?.id && now?.email == beforeIdent?.email
            let nowName = nowAccount?.displayName ?? now?.email ?? now?.id
            Log.write("ERROR codex switch to “\(target.displayName)” failed (rc=\(r.exitCode), auth.json \(unchanged ? "unchanged" : "now has \(now?.id ?? (now == nil ? "nobody" : "an e-mail-only account"))"))")
            if !unchanged, let nowAccount {
                UsageCollector.triggerAfterSwitch(uuid: nowAccount.stateUuid, label: nowAccount.displayName, provider: "codex")
            }
            let detail = clip(r.stderr.isEmpty ? r.stdout : r.stderr)
            let state: String
            if now == nil {
                state = L("Codex has no signed-in account now.", "Сейчас в Codex не выполнен вход ни в один аккаунт.")
            } else if unchanged {
                state = L("Codex is still signed in to “\(nowName ?? "?")”, as before.",
                          "В Codex по-прежнему аккаунт «\(nowName ?? "?")».")
            } else {
                state = L("Codex is now signed in to “\(nowName ?? "?")”.", "Сейчас в Codex аккаунт «\(nowName ?? "?")».")
            }
            let t3Note: String
            if t3LaunchFailed {
                t3Note = L("\n\nT3 Code did not reopen automatically. Open it manually (details are in the log).",
                           "\n\nT3 Code не смог открыться сам. Откройте его вручную (подробности в журнале).")
            } else if t3WasOpen {
                t3Note = L("\n\nT3 Code has been restarted and will use the account Codex has now.",
                           "\n\nT3 Code перезапущен и будет работать с аккаунтом, который сейчас в Codex.")
            } else {
                t3Note = ""
            }
            alert(L("Could not switch Codex", "Не удалось переключить Codex"),
                  (r.ok ? L("CodexBar reported success, but Codex is not signed in to “\(target.displayName)”.",
                            "CodexBar сообщил об успехе, но в Codex не аккаунт «\(target.displayName)».")
                        : L("CodexBar could not make “\(target.displayName)” the Codex account (exit code \(r.exitCode)).",
                            "CodexBar не смог сделать «\(target.displayName)» аккаунтом Codex (код \(r.exitCode)).")) +
                  "\n\n" + state +
                  (detail.isEmpty ? "" : "\n\n" + detail) +
                  t3Note)
            return
        }
        let landedAccount = CodexAccounts.discover().first { $0.isActive }
        UsageCollector.triggerAfterSwitch(uuid: landedAccount?.stateUuid ?? target.stateUuid, label: target.displayName, provider: "codex")
        if t3LaunchFailed {
            alert(L("Codex switched, but T3 Code did not reopen", "Codex переключён, но T3 Code не открылся"),
                  L("Codex is now signed in to “\(target.displayName)”, but T3 Code did not start after 3 attempts. Check the log (menu → “Open log”).",
                    "В Codex теперь аккаунт «\(target.displayName)», но T3 Code не запустился после 3 попыток. Проверьте журнал (меню → «Открыть журнал»)."))
        }
        Log.write("codex switch: “\(target.displayName)” ready (T3Code \(t3WasOpen ? (t3LaunchFailed ? "relaunch failed" : "relaunched") : "kept closed"))")
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

    private static func displayProvider(_ raw: String) -> String {
        switch raw.lowercased() {
        case "claudeagent", "claude": return "Claude"
        case "codex": return "Codex"
        case "antigravity": return "Antigravity"
        case "cursor": return "Cursor"
        case "opencode": return "OpenCode"
        case "grok": return "Grok"
        case "": return ""
        default: return raw
        }
    }

    func confirmBusyT3RestartIfNeeded() -> Bool {
        guard T3Code.running != nil, !fakeClaude, profiles.store.askBeforeRestartingBusyT3 else { return true }
        let runs = T3Code.activeRuns()
        guard !runs.isEmpty else { return true }
        let list = runs.prefix(6).map { r -> String in
            let title = r.title.isEmpty ? r.threadId : r.title
            let prov = Self.displayProvider(r.provider)
            return prov.isEmpty ? "• \(title)" : "• \(title) — \(prov)"
        }.joined(separator: "\n")
        let extra = runs.count > 6
            ? L("\n…and \(runs.count - 6) more", "\n…и ещё \(runs.count - 6)")
            : ""
        let a = NSAlert()
        a.messageText = L("T3 Code has active tasks (\(runs.count))", "В T3 Code сейчас работают чаты (\(runs.count))")
        a.informativeText = L(
            "Switching the account will restart T3 Code and interrupt these active tasks:\n\n\(list)\(extra)",
            "Смена аккаунта перезапустит T3 Code и прервёт эти активные задачи:\n\n\(list)\(extra)"
        )
        a.showsSuppressionButton = true
        a.suppressionButton?.title = L("Do not ask again (always restart T3 Code)", "Больше не спрашивать (всегда перезапускать T3 Code)")
        a.addButton(withTitle: L("Restart and switch", "Перезапустить и переключить"))
        a.addButton(withTitle: L("Cancel", "Отмена"))
        NSApp.activate(ignoringOtherApps: true)
        let ok = a.runModal() == .alertFirstButtonReturn
        if ok, a.suppressionButton?.state == .on {
            profiles.store.askBeforeRestartingBusyT3 = false
            profiles.save()
            Log.write("t3: user disabled busy-restart confirmation")
        }
        if !ok { Log.write("switch aborted: user cancelled busy T3 Code restart") }
        return ok
    }

    func startNewAccount(viaHapp: Bool) {
        Task {
            guard !busy else { return }
            guard confirmBusyT3RestartIfNeeded() else { return }
            Log.write("new account sign-in\(viaHapp ? " via Happ VPN" : ""): starting")
            busy = true; defer { busy = false }
            autosaveWork?.cancel()
            let leaving = profiles.currentUuid().flatMap { profiles.account(withUuid: $0) } ?? profiles.active
            let claudeWasOpen = Claude.running != nil
            let t3WasOpen = T3Code.running != nil
            if claudeWasOpen { guard await Claude.quit() else { return } }
            if t3WasOpen { guard await T3Code.quit() else { if claudeWasOpen { Claude.launch() }; return } }
            if viaHapp {
                guard await Happ.connect() else {
                    alert(L("Happ did not connect", "Happ не подключился"),
                          L("Signing in to a new account was cancelled so that you do not sign in without the VPN. Details are in the log.",
                            "Вход в новый аккаунт отменён, чтобы не входить без VPN. Подробности в журнале."))
                    if leaving?.useHapp != true {
                        if claudeWasOpen { Claude.launch() }
                        if t3WasOpen { T3Code.launch() }
                    }
                    return
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
                profiles.cliBackupIfNeeded()
                try saveCurrentBeforeLeaving()
                _ = profiles.syncLiveCliToMatchingProfile()
                try profiles.restore(nil)
                _ = try ClaudeCLI.restore(account: nil, profileDir: nil)
                profiles.store.activeId = nil; profiles.save()
            } catch {
                Log.write("ERROR signing in to a new account: \(error.localizedDescription)")
                alert(L("Error", "Ошибка"), error.localizedDescription)
                if claudeWasOpen { Claude.launch() }
                if t3WasOpen { T3Code.launch() }
                return
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

    @discardableResult
    func switchTo(_ target: Account, forceLaunchClaude: Bool = false) async -> Bool {
        guard !busy else { Log.write("switch to “\(target.name)” rejected: another one is in progress"); return false }
        _ = profiles.syncLiveCliToMatchingProfile()
        let curUuid = profiles.currentUuid()
        let curCliUuid = ClaudeCLI.currentUuid()
        let desktopMatches = (curUuid != nil && curUuid == target.accountUuid)
        let cliMatches = (curCliUuid != nil && curCliUuid == target.accountUuid)
        let claudeWasOpen = Claude.running != nil
        let t3WasOpen = T3Code.isRunningNow
        Log.write("switch: “\(target.name)” (\(target.accountUuid ?? "-")), in Claude now \(curUuid ?? "nobody"), in CLI now \(curCliUuid ?? "nobody"), Claude \(claudeWasOpen ? "open" : "closed"), T3Code \(t3WasOpen ? "open" : "closed")")
        // Taken before the first await: while Happ's service is being checked the main actor is free,
        // and a second switch (or an autosave) must not start in that gap.
        busy = true; defer { busy = false }
        await Happ.ensureService()
        let dead = target.needsRelogin == true
        let needVPN = target.useHapp == true
        let relaunchApps = { [weak self] (launchClaude: Bool, launchT3: Bool) in
            guard let self else { return }
            if launchClaude {
                self.suppressLaunchPromptUntil = Date().addingTimeInterval(30)
                Claude.launch(onConfirmedPid: { [weak self] pid in self?.managedClaudeLaunchPids.insert(pid) })
            }
            if launchT3 {
                self.suppressT3LaunchPromptUntil = Date().addingTimeInterval(30)
                T3Code.launch(canRetry: { !self.busy }, onConfirmedPid: { [weak self] pid in self?.managedT3LaunchPids.insert(pid) })
            }
        }

        if desktopMatches && cliMatches {
            if needVPN, !Happ.isConnected() {
                Log.write("switch: this is the current account, but Happ is not connected; reconnecting")
                guard confirmBusyT3RestartIfNeeded() else { return false }
                async let directIPTask = Happ.publicIP()
                async let qClaude = claudeWasOpen ? Claude.quit() : true
                async let qT3 = t3WasOpen ? T3Code.quit() : true
                let (cOk, tOk, preDirect) = await (qClaude, qT3, directIPTask)
                guard cOk && tOk else {
                    if claudeWasOpen && cOk && !tOk { relaunchApps(true, false) }
                    return false
                }
                guard await Happ.connect(prefetchedDirectIP: preDirect) else {
                    alert(L("Happ did not connect", "Happ не подключился"),
                          L("Apps were not reopened so that you do not sign in to “\(target.name)” without the VPN. Connect Happ manually and choose the account again. Details are in the log.",
                            "Приложения не открыты, чтобы не входить в «\(target.name)» без VPN. Подключите Happ вручную и выберите аккаунт ещё раз. Подробности в журнале."))
                    return false
                }
                relaunchApps(claudeWasOpen || forceLaunchClaude, t3WasOpen)
                UsageCollector.triggerAfterSwitch(account: target)
                return true
            }
            Log.write("switch: this is already the current account in both Desktop and CLI")
            if forceLaunchClaude && !claudeWasOpen {
                relaunchApps(true, false)
            } else if claudeWasOpen && !t3WasOpen {
                Claude.running?.activate()
            }
            return true
        }

        // Ask before interrupting an active generation in T3 Code (unless user disabled the warning).
        if t3WasOpen {
            guard confirmBusyT3RestartIfNeeded() else { return false }
        }

        if dead { Log.write("switch: the saved sign-in for “\(target.name)” is stale, opening the sign-in screen") }
        autosaveWork?.cancel()
        // Quit open apps in parallel, and prefetch direct IP concurrently when switching to a VPN account.
        let shouldPrefetchDirectIP = needVPN && !Happ.isConnected()
        async let preDirectTask: Happ.IPInfo? = shouldPrefetchDirectIP ? Happ.publicIP() : nil
        async let quitClaudeTask = claudeWasOpen ? Claude.quit() : true
        async let quitT3Task = t3WasOpen ? T3Code.quit() : true
        let (claudeQuitOk, t3QuitOk, prefetchedDirectIP) = await (quitClaudeTask, quitT3Task, preDirectTask)
        guard claudeQuitOk else {
            Log.write("switch aborted: Claude did not quit")
            if t3WasOpen && t3QuitOk { relaunchApps(false, true) }
            return false
        }
        guard t3QuitOk else {
            Log.write("switch aborted: T3 Code did not quit")
            if claudeWasOpen { relaunchApps(true, false) }
            return false
        }
        let leaving = curUuid.flatMap { profiles.account(withUuid: $0) } ?? profiles.active
        let leavingCli = curCliUuid.flatMap { profiles.account(withUuid: $0) }
        let leavingNeedsVPN = (leaving?.useHapp == true) || (leavingCli?.useHapp == true)
        if needVPN {
            guard Happ.installed else {
                Log.write("ERROR: Happ is not installed")
                alert(L("Happ not found", "Happ не найден"),
                      L("“\(target.name)” needs the Happ VPN, but the app is not in /Applications.",
                        "Для «\(target.name)» нужен VPN Happ, но приложения нет в /Applications."))
                if !leavingNeedsVPN { relaunchApps(claudeWasOpen, t3WasOpen) }
                return false
            }
            guard await Happ.connect(prefetchedDirectIP: prefetchedDirectIP) else {
                alert(L("Happ did not connect", "Happ не подключился"),
                      L("Switching to “\(target.name)” was cancelled so that you do not sign in without the VPN. Details are in the log (menu → “Open log”).",
                        "Переключение на «\(target.name)» отменено, чтобы не входить без VPN. Подробности в журнале (меню → «Открыть журнал»)."))
                if !leavingNeedsVPN { relaunchApps(claudeWasOpen, t3WasOpen) }
                return false
            }
        } else if leavingNeedsVPN || (Happ.app != nil && profiles.store.accounts.contains { $0.useHapp == true }) {
            // Leaving a VPN account (or Happ stayed open after one): quit Happ and make sure the IP is direct again.
            guard await Happ.shutdown() else {
                alert(L("Happ did not quit", "Happ не закрылся"),
                      L("Switching to “\(target.name)” was stopped so that this account does not sign in from the VPN IP. Quit Happ manually and choose the account again.",
                        "Переключение на «\(target.name)» остановлено, чтобы этот аккаунт не зашёл с IP VPN. Закройте Happ вручную и выберите аккаунт ещё раз."))
                return false
            }
        }
        Log.write("switch: apps closed (Claude \(Claude.running == nil ? "ok" : "NO!"), T3Code \(T3Code.running == nil ? "ok" : "NO!")), network ready, putting “\(target.name)” in place")
        var restoreStarted = false
        var cliRestored = false
        do {
            try profiles.initialBackupIfNeeded()
            profiles.cliBackupIfNeeded()
            try saveCurrentBeforeLeaving()
            _ = profiles.syncLiveCliToMatchingProfile()
            restoreStarted = true
            if !desktopMatches || dead {
                try profiles.restore(dead ? nil : target)
            }
            cliRestored = try ClaudeCLI.restore(account: dead ? nil : target, profileDir: dead ? nil : profiles.profileDir(target))
            profiles.store.activeId = target.id; profiles.save()
        } catch {
            Log.write("ERROR switching: \(error.localizedDescription)")
            // A restore that failed halfway leaves files of two accounts in the Claude folder: put the previous account back.
            var rolledBack = !restoreStarted
            if restoreStarted {
                do {
                    if !desktopMatches { try profiles.restore(leaving) }
                    _ = try ClaudeCLI.restore(account: leavingCli, profileDir: leavingCli.map { profiles.profileDir($0) })
                    rolledBack = true
                    Log.write("switch: the files of “\(leaving?.name ?? "empty login")” are back in place")
                } catch let rollbackError {
                    Log.write("ERROR rolling back: \(rollbackError.localizedDescription)")
                }
            }
            // The network is already set up for the target, so apps reopen only if the previous account
            // lives on the same side of the VPN. Otherwise they stay closed: never a mixed login, never the wrong IP.
            let sameSide = (leaving?.useHapp == true) == needVPN
            let reopen = rolledBack && sameSide
            alert(L("Could not switch", "Не удалось переключить"),
                  error.localizedDescription + (reopen ? "" : L("\n\nApps were not reopened. Choose the account again.",
                                                               "\n\nПриложения не открыты. Выберите аккаунт ещё раз.")))
            if reopen { relaunchApps(claudeWasOpen, t3WasOpen) }
            updateTooltip()
            return false
        }
        if !dead, !desktopMatches, let u = target.accountUuid {
            expecting = (u, Date().addingTimeInterval(120))
            DispatchQueue.main.asyncAfter(deadline: .now() + 120) { [weak self] in
                MainActor.assumeIsolated {
                    guard let self, self.expecting?.uuid == u else { return }
                    self.expecting = nil
                    Log.write("switch confirmed: Claude did not sign out of “\(target.name)” within 2 minutes, config.json has \(self.profiles.currentUuid() ?? "nobody")")
                }
            }
        }

        let shouldLaunchClaude = claudeWasOpen || forceLaunchClaude || (dead && !t3WasOpen)
        relaunchApps(shouldLaunchClaude, false)
        updateTooltip()

        // If the target has no saved CLI login yet, offer a one-time `claude auth login` right now (VPN is already set up).
        if !cliRestored && !fakeClaude && !dead && (t3WasOpen || !claudeWasOpen) {
            let a = NSAlert()
            a.messageText = L("One-time CLI sign-in needed for “\(target.name)”",
                              "Нужен разовый вход в CLI / T3 Code для «\(target.name)»")
            a.informativeText = L(
                "Account switched, but Claude Code CLI (used by T3 Code) does not have a saved sign-in for “\(target.name)” yet.\n\nClick “Sign in now” to open browser sign-in\(needVPN ? " (through Happ VPN)" : ""). After you finish, the switcher saves it automatically and won't ask again.",
                "Аккаунт переключён, но для «\(target.name)» ещё не сохранён вход Claude Code CLI (который использует T3 Code).\n\nНажмите «Войти сейчас» — откроется браузер для входа\(needVPN ? " (уже через VPN Happ)" : ""). После входа переключатель сам сохранит токен и больше спрашивать не будет."
            )
            a.addButton(withTitle: L("Sign in now", "Войти сейчас"))
            a.addButton(withTitle: L("Later", "Позже"))
            NSApp.activate(ignoringOtherApps: true)
            if a.runModal() == .alertFirstButtonReturn {
                await performCliLogin(for: target, relaunchT3: t3WasOpen)
                UsageCollector.triggerAfterSwitch(account: target)
                return true
            }
        }
        if t3WasOpen { relaunchApps(false, true) }
        UsageCollector.triggerAfterSwitch(account: target)
        Log.write("switch: “\(target.name)” ready (Claude \(shouldLaunchClaude ? "launched" : "kept closed"), T3Code \(t3WasOpen ? "launched" : "kept closed"), CLI \(cliRestored ? "restored" : "cleared"))")
        return true
    }

    func performCliLogin(for target: Account, relaunchT3: Bool) async {
        if !ClaudeCLI.launchTerminalLogin(for: target, relaunchT3: relaunchT3) {
            alert(L("Could not open Terminal", "Не удалось открыть Терминал"),
                  L("Run `claude auth login --claudeai` in Terminal manually.", "Выполните `claude auth login --claudeai` в Терминале вручную."))
            if relaunchT3 {
                suppressT3LaunchPromptUntil = Date().addingTimeInterval(30)
                _ = await T3Code.launchAndWait(onConfirmedPid: { [weak self] pid in
                    self?.managedT3LaunchPids.insert(pid)
                })
            }
        }
        updateTooltip()
    }

    @objc func signInCliForAccount(_ sender: NSMenuItem) {
        guard let id = sender.representedObject as? String,
              let a = profiles.store.accounts.first(where: { $0.id == id }) else { return }
        Task {
            guard !busy else { return }
            let curUuid = profiles.currentUuid()
            let needVPN = a.useHapp == true
            if curUuid != a.accountUuid || (needVPN && !Happ.isConnected()) || (!needVPN && Happ.isConnected()) {
                // Switch first so VPN state matches the target account before browser login.
                guard await switchTo(a) else { return }
                // If switchTo already prompted & logged in, we're done.
                if profiles.hasCliLogin(a) && ClaudeCLI.currentUuid() == a.accountUuid { return }
            }
            guard !busy else { return }
            let t3WasOpen = T3Code.isRunningNow
            if t3WasOpen { guard confirmBusyT3RestartIfNeeded() else { return } }
            busy = true; defer { busy = false }
            if t3WasOpen { guard await T3Code.quit() else { return } }
            await performCliLogin(for: a, relaunchT3: t3WasOpen)
        }
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

    @objc func toggleAskBusyT3() { profiles.store.askBeforeRestartingBusyT3.toggle(); profiles.save() }

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
                UsageCollector.triggerAfterSwitch(account: a)
            } else {
                guard !declinedUuids.contains(uuid), !promptOpen else { return }
                promptOpen = true; defer { promptOpen = false }
                if let (name, vpn) = askNewAccount(L("Claude is signed in to a new account", "В Claude выполнен вход в новый аккаунт"),
                                  L("It is not in the switcher's list. What should it be called? (Cancel: do not save.)",
                                    "Его нет в списке переключателя. Как его назвать? (Отмена — не сохранять.)")) {
                    let saved = try profiles.saveCurrent(name: name)
                    setHapp(name: name, vpn)
                    Log.write("autosave: added account “\(name)” \(uuid)")
                    UsageCollector.triggerAfterSwitch(account: saved)
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
        guard let app = n.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication else { return }
        if app.bundleIdentifier == claudeBundleID {
            guard Claude.isMainProcess(app) else {
                Log.write("launch under the Claude id ignored: not Claude but \(app.executableURL?.path ?? "an unknown file") (pid \(app.processIdentifier))")
                return
            }
            if managedClaudeLaunchPids.remove(app.processIdentifier) != nil { return }
            guard !busy, Date() > suppressLaunchPromptUntil else { return }
            if profiles.store.askOnClaudeLaunch, profiles.store.accounts.count >= 2,
               showPicker(reason: L("Claude is starting. Which account should it open?", "Claude запускается. В какой аккаунт зайти?"), forceLaunchClaude: true) { return }
            // Claude was opened manually in an account that needs the VPN, and Happ is not connected: quit, connect, reopen.
            if let cur = profiles.currentUuid().flatMap({ profiles.account(withUuid: $0) }), cur.useHapp == true, !Happ.isConnected() {
                Log.write("Claude opened in “\(cur.name)” without the VPN: quitting, connecting Happ, reopening")
                Task { await switchTo(cur, forceLaunchClaude: true) }
            }
        } else if app.bundleIdentifier == t3BundleID {
            guard T3Code.isMainProcess(app) else { return }
            if managedT3LaunchPids.remove(app.processIdentifier) != nil { return }
            guard !busy, Date() > suppressT3LaunchPromptUntil else { return }
            let cliAcc = ClaudeCLI.currentUuid().flatMap { profiles.account(withUuid: $0) }
            if let cur = cliAcc, cur.useHapp == true, !Happ.isConnected() {
                Log.write("T3 Code opened in “\(cur.name)” without the VPN: quitting, connecting Happ, reopening")
                Task { await switchTo(cur) }
            }
        }
    }

    /// Returns true if an account was chosen (the switch has started).
    @discardableResult
    func showPicker(reason: String, forceLaunchClaude: Bool = false) -> Bool {
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
        Task { await switchTo(ordered[r], forceLaunchClaude: forceLaunchClaude) }
        return true
    }

    // MARK: dialogs

    func alert(_ title: String, _ text: String) {
        let clipped = text.count > 500 ? String(text.prefix(500)) + "…" : text
        if fakeClaude { Log.write("ALERT: \(title) — \(clipped)"); lastAlert = title; lastAlertText = text; return }
        let a = NSAlert(); a.messageText = title; a.informativeText = clipped
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
//   ClaudeSwitcher codex-list [--json]    the Codex accounts found on this Mac (read-only, no tokens)
//   ClaudeSwitcher codex-selftest         Codex discovery and switching on fake files and a fake CodexBarCLI

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
        e["CLAUDE_SWITCHER_CLI_JSON"] = root.appendingPathComponent("claude.json").path
        e["CLAUDE_SWITCHER_CLI_KEYCHAIN_FILE"] = root.appendingPathComponent("keychain.json").path
        p.environment = e
        try! p.run(); p.waitUntilExit(); exit(p.terminationStatus)
    }
    try! fm.createDirectory(at: claudeDir, withIntermediateDirectories: true)
    let accts: [(String, String, Bool)] = [("A-no-VPN", "aaaaaaaa-0000-0000-0000-000000000001", false),
                                           ("B-no-VPN", "bbbbbbbb-0000-0000-0000-000000000002", false),
                                           ("C-VPN", "cccccccc-0000-0000-0000-000000000003", true)]
    func fakeLogin(_ uuid: String, _ name: String) {
        try! "cookies-\(uuid)".write(to: claudeDir.appendingPathComponent("Cookies"), atomically: true, encoding: .utf8)
        let cfg: [String: Any] = ["lastKnownAccountUuid": uuid, "oauth:tokenCache": "token-\(uuid)", "userThemeMode": "dark"]
        try! JSONSerialization.data(withJSONObject: cfg).write(to: configFile)
        // Also populate fake CLI auth + shared settings + mcpOAuth to verify surgical swapping.
        let cliCfg: [String: Any] = [
            "sharedSetting": "keep-me",
            "oauthAccount": ["accountUuid": uuid, "emailAddress": "\(name)@example.com"],
        ]
        try! ClaudeCLI.writeJson(cliCfg)
        var kc = ClaudeCLI.readLiveCredentialsDict()
        kc["mcpOAuth"] = ["serverA": ["token": "mcp-keep"]]
        kc["claudeAiOauth"] = ["accessToken": "cli-token-\(uuid)", "refreshToken": "cli-refresh-\(uuid)"]
        let kcData = try! JSONSerialization.data(withJSONObject: kc)
        try! ClaudeCLI.writeKeychain(service: ClaudeCLI.liveKeychainService, value: String(decoding: kcData, as: UTF8.self))
    }
    var failures = 0
    func check(_ ok: Bool, _ what: String) { print(ok ? "  ✓" : "  ✗ FAILED:", what); if !ok { failures += 1 } }
    print("Self-test. Folder: \(claudeDir.deletingLastPathComponent().path)")
    MainActor.assumeIsolated {
        let d = AppDelegate()
        for (name, uuid, vpn) in accts {
            fakeLogin(uuid, name); try! d.profiles.saveCurrent(name: name)
            _ = d.profiles.syncLiveCliToMatchingProfile()
            if vpn { d.profiles.store.accounts[d.profiles.store.accounts.count - 1].useHapp = true; d.profiles.save() }
        }
        // Start as in real life: account A is in Claude & CLI.
        try! d.profiles.restore(d.profiles.store.accounts[0])
        _ = try! ClaudeCLI.restore(account: d.profiles.store.accounts[0], profileDir: d.profiles.profileDir(d.profiles.store.accounts[0]))
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
                let cliJson = ClaudeCLI.readJson()
                let cliKc = ClaudeCLI.readLiveCredentialsDict()
                check(ClaudeCLI.currentUuid() == t.accountUuid, "CLI oauthAccount has \(name)")
                check((cliKc["claudeAiOauth"] as? [String: Any])?["accessToken"] as? String == "cli-token-\(t.accountUuid!)", "CLI Keychain token belongs to \(name)")
                check(cliJson["sharedSetting"] as? String == "keep-me", "shared ~/.claude.json keys untouched")
                check(((cliKc["mcpOAuth"] as? [String: Any])?["serverA"] as? [String: Any])?["token"] as? String == "mcp-keep", "Keychain mcpOAuth untouched")
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

if args.count >= 2, args[1] == "codex-list" {
    let list = CodexAccounts.discover()
    if args.contains("--json") {
        let rows: [[String: Any]] = list.map { c in
            ["accountId": c.accountId ?? NSNull(), "email": c.email ?? NSNull(), "displayName": c.displayName,
             "managedId": c.managedId ?? NSNull(), "plan": c.planName ?? NSNull(), "active": c.isActive,
             "promoteTarget": c.promoteTarget ?? NSNull(), "sources": c.sources]
        }
        let data = try! JSONSerialization.data(withJSONObject: rows, options: [.prettyPrinted, .sortedKeys])
        print(String(decoding: data, as: UTF8.self))
    } else {
        print("codex home:", CodexAccounts.codexHome.path, "| CodexBarCLI:", fm.isExecutableFile(atPath: CodexAccounts.cliPath) ? "found" : "missing")
        if list.isEmpty { print("no Codex accounts (the menu hides the Codex section)") }
        for c in list {
            print(c.isActive ? "✓" : " ", c.displayName, "<\(c.email ?? "-")>", c.accountId ?? "-",
                  "plan=\(c.planName ?? "-")", "managed=\(c.managedId ?? "-")", "from=\(c.sources.joined(separator: ","))")
        }
    }
    exit(0)
}

// Codex accounts on made-up files in a temporary folder: discovery rules and switchCodexTo with a fake
// CodexBarCLI (never the real ~/.codex/auth.json, CodexBar or T3 Code). Run: ClaudeSwitcher codex-selftest
if args.count >= 2, args[1] == "codex-selftest" {
    let env = ProcessInfo.processInfo.environment
    let childMarker = "CLAUDE_SWITCHER_CODEX_SELFTEST_CHILD", rootKey = "CLAUDE_SWITCHER_CODEX_SELFTEST_ROOT"
    /// Every file the Codex code writes or deletes, resolved strictly inside `root` (no "..", no symlink out).
    func outsideRoot(_ root: String) -> [String] {
        let r = URL(fileURLWithPath: root).standardizedFileURL.resolvingSymlinksInPath().path
        // a folder this self-test made: claude-switcher-codex-selftest-<id>[/<run>]
        guard URL(fileURLWithPath: r).pathComponents.dropLast().last.map({ $0.hasPrefix("claude-switcher-codex-selftest-") }) == true
              || URL(fileURLWithPath: r).lastPathComponent.hasPrefix("claude-switcher-codex-selftest-") else { return ["root \(root)"] }
        return [CodexAccounts.authFile, CodexAccounts.managedFile, CodexAccounts.managedHomes,
                URL(fileURLWithPath: CodexAccounts.cliPath), CodexAccounts.namesFile, Log.file].compactMap { u in
            let std = u.standardizedFileURL
            // the deepest existing folder, resolved: a symlink there must not lead out of the root
            var dir = std.deletingLastPathComponent()
            while !fm.fileExists(atPath: dir.path), dir.path != "/" { dir = dir.deletingLastPathComponent() }
            let resolved = dir.resolvingSymlinksInPath().path + String(std.path.dropFirst(dir.path.count))
            let inside = { (x: String) in x.hasPrefix(r + "/") || x.hasPrefix(root + "/") }
            return inside(std.path) && inside(resolved) && !std.pathComponents.contains("..") ? nil : std.path
        }
    }
    if env[childMarker] != "1" {
        let root = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("claude-switcher-codex-selftest-\(UUID().uuidString.prefix(8))")
        func child(_ sub: String, override: [String: String] = [:], drop: [String] = []) -> Int32 {
            let base = root.appendingPathComponent(sub)
            let p = Process(); p.executableURL = URL(fileURLWithPath: CommandLine.arguments[0]); p.arguments = ["codex-selftest"]
            var e = env
            e[childMarker] = "1"
            e[rootKey] = base.path
            // the real Claude and T3 Code are never touched by the child either
            e["CLAUDE_SWITCHER_FAKE_CLAUDE"] = "1"
            for (k, s) in [("CLAUDE_SWITCHER_CLAUDE_DIR", "claude"), ("CLAUDE_SWITCHER_STORE_DIR", "store"),
                           ("CLAUDE_SWITCHER_CLI_JSON", "claude.json"), ("CLAUDE_SWITCHER_CLI_KEYCHAIN_FILE", "keychain.json"),
                           ("CLAUDE_SWITCHER_LOG_DIR", "logs"), ("CLAUDE_SWITCHER_CODEX_HOME", "codex"),
                           ("CLAUDE_SWITCHER_CODEXBAR_DIR", "codexbar"), ("CLAUDE_SWITCHER_CODEXBAR_CLI", "fake-codexbar-cli")] {
                e[k] = base.appendingPathComponent(s).path
            }
            e.removeValue(forKey: "CODEX_HOME")
            for (k, v) in override { e[k] = v }
            for k in drop { e.removeValue(forKey: k) }
            p.environment = e
            p.standardOutput = sub == "run" ? FileHandle.standardOutput : FileHandle.nullDevice
            try! p.run(); p.waitUntilExit()
            return p.terminationStatus
        }
        var bad = 0
        func check(_ ok: Bool, _ what: String) { print(ok ? "  ✓" : "  ✗ FAILED:", what); if !ok { bad += 1 } }
        print("Codex self-test, child-root safety (the child refuses to run with any path outside its root):")
        let outside = root.appendingPathComponent("outside-codex")
        check(child("guard1", override: ["CLAUDE_SWITCHER_CODEX_HOME": outside.path]) == 1
              && !fm.fileExists(atPath: outside.appendingPathComponent("auth.json").path)
              && !fm.fileExists(atPath: root.appendingPathComponent("guard1/codexbar").path),
              "CODEX_HOME outside the root: exit 1 before writing anything")
        check(child("guard2", override: ["CLAUDE_SWITCHER_CODEXBAR_DIR": root.appendingPathComponent("guard2/../elsewhere").path]) == 1
              && !fm.fileExists(atPath: root.appendingPathComponent("elsewhere").path), "a “..” path out of the root: exit 1")
        check(child("guard3", drop: [rootKey]) == 1, "no root given: exit 1")
        check(child("guard4", override: [rootKey: NSHomeDirectory()]) == 1, "the home folder as the root: exit 1")
        let rc = child("run")
        try? fm.removeItem(at: root)
        if bad > 0 { print("RESULT: \(bad) child-root safety failures") }
        exit(rc == 0 && bad == 0 ? 0 : 1)
    }
    guard let selftestRoot = env[rootKey], outsideRoot(selftestRoot).isEmpty else {
        FileHandle.standardError.write(Data("codex-selftest: refusing to run, outside \(env[rootKey] ?? "(no root)"): \(env[rootKey].map(outsideRoot) ?? [])\n".utf8))
        exit(1)
    }
    let root = URL(fileURLWithPath: selftestRoot)
    var failures = 0
    func check(_ ok: Bool, _ what: String) { print(ok ? "  ✓" : "  ✗ FAILED:", what); if !ok { failures += 1 } }
    func jwt(_ claims: [String: Any]) -> String {
        func enc(_ o: [String: Any]) -> String {
            try! JSONSerialization.data(withJSONObject: o).base64EncodedString()
                .replacingOccurrences(of: "+", with: "-").replacingOccurrences(of: "/", with: "_").replacingOccurrences(of: "=", with: "")
        }
        return "\(enc(["alg": "none"])).\(enc(claims)).sig"
    }
    func writeAuth(_ url: URL, _ id: String?, _ email: String, _ plan: String) {
        try! fm.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        var tokens: [String: Any] = ["refresh_token": "never-read",
            "id_token": jwt(["email": email, "https://api.openai.com/auth": ["chatgpt_plan_type": plan].merging(id.map { ["chatgpt_account_id": $0] } ?? [:]) { a, _ in a }])]
        if let id { tokens["account_id"] = id }
        let obj: [String: Any] = ["auth_mode": "chatgpt", "tokens": tokens]
        try! JSONSerialization.data(withJSONObject: obj).write(to: url)
    }
    let A = "aaaaaaaa-0000-4000-8000-00000000000a", B = "bbbbbbbb-0000-4000-8000-00000000000b"
    let C = "cccccccc-0000-4000-8000-0000000000cc", D = "dddddddd-0000-4000-8000-0000000000dd"
    let homes = CodexAccounts.managedHomes
    writeAuth(CodexAccounts.authFile, A, "plus.user@example.com", "plus")
    writeAuth(homes.appendingPathComponent("M2/auth.json"), B, "pro.user@example.com", "prolite")
    let managed: [String: Any] = ["version": 3, "accounts": [
        ["id": "M1", "email": "Plus.User@Example.com", "providerAccountID": A.uppercased(), "workspaceAccountID": A.uppercased(),
         "managedHomePath": homes.appendingPathComponent("M1").path],
        ["id": "M2", "email": "pro.user@example.com", "providerAccountID": B, "workspaceAccountID": B,
         "managedHomePath": homes.appendingPathComponent("M2").path],
        ["id": "M3", "email": "third@example.com", "providerAccountID": C, "managedHomePath": "/etc"],
        ["id": "M4", "email": "fourth@example.com", "providerAccountID": D]]]
    try! JSONSerialization.data(withJSONObject: managed).write(to: CodexAccounts.managedFile)
    try! fm.createDirectory(at: CodexAccounts.namesFile.deletingLastPathComponent(), withIntermediateDirectories: true)
    try! "{\"11111111-claude-account\": \"Claude\", \"\(B)\": \"Second\", \"\(A)\": \"First\"}".write(to: CodexAccounts.namesFile, atomically: true, encoding: .utf8)
    // What `promote <id>` would put in place; M3 fails, M4 exits 0 without changing anything.
    writeAuth(root.appendingPathComponent("promote/M1.json"), A, "plus.user@example.com", "plus")
    writeAuth(root.appendingPathComponent("promote/M2.json"), B, "pro.user@example.com", "prolite")
    // e-mail-only targets: M5 lands (auth.json with that e-mail, no id), M6 exits 0 but leaves B's login in place;
    // M7 (workspace W7, provider id P7) lands with an auth.json that names its e-mail only
    let W7 = "77777777-0000-4000-8000-000000000077"
    writeAuth(root.appendingPathComponent("promote/M5.json"), nil, "fifth@example.com", "plus")
    writeAuth(root.appendingPathComponent("promote/M6.json"), B, "pro.user@example.com", "prolite")
    writeAuth(root.appendingPathComponent("promote/M7.json"), nil, "seventh@example.com", "plus")
    let script = """
        #!/bin/sh
        [ "$1 $2 $4" = "codex-accounts promote --json" ] || { echo "bad arguments: $*" >&2; exit 64; }
        [ "$CODEX_HOME" = "\(CodexAccounts.codexHome.path)" ] || { echo "CODEX_HOME is not the switcher's: $CODEX_HOME" >&2; exit 65; }
        case "$3" in
          M3) echo "Error: account changed concurrently" >&2; exit 3 ;;
          M4) echo '{"promoted": true}'; exit 0 ;;
        esac
        cp "\(root.path)/promote/$3.json" "\(CodexAccounts.authFile.path)" && echo '{"promoted": true}'
        """
    try! script.write(to: URL(fileURLWithPath: CodexAccounts.cliPath), atomically: true, encoding: .utf8)
    try! fm.setAttributes([.posixPermissions: 0o755], ofItemAtPath: CodexAccounts.cliPath)

    print("Codex self-test. Folder: \(root.path)")
    check(CodexAccounts.orderedKeys("{\"b\": \"x\", \"a\": {\"z\": 1}, \"c\": \"y\\\"q\"}") == ["b", "a", "c"], "names-file keys in file order")
    var list = CodexAccounts.discover()
    check(list.map(\.accountId) == [B, A, D, C], "one entry per lower-case id, names-file order, then e-mail")
    check(list.map(\.displayName) == ["Second", "First", "fourth@example.com", "third@example.com"], "display name, else the e-mail")
    check(list.map(\.isActive) == [false, true, false, false], "active = the account of auth.json")
    func byId(_ id: String) -> CodexAccount { list.first { $0.accountId == id }! }
    check(list[1].managedId == "M1" && list[1].sources.sorted() == ["live", "managed"], "the live account keeps its managed UUID")
    check(byId(B).planName == "Pro 5x" && byId(A).planName == "Plus" && byId(C).planName == nil, "plans: prolite = Pro 5x; no claim from a home outside CodexBar's folder")
    MainActor.assumeIsolated {
        let d = AppDelegate()
        // the real menu, built as when it opens: Codex section below the Claude one, before "Sign in…"
        let menu = NSMenu()
        d.menuNeedsUpdate(menu)
        let titles = menu.items.map { $0.isSeparatorItem ? "---" : $0.title }
        let h = titles.firstIndex(of: L("Codex accounts", "Аккаунты Codex")) ?? -1
        let signIn = titles.firstIndex(of: L("Sign in to a new account…", "Войти в новый аккаунт…")) ?? -1
        let claudeHeader = titles.firstIndex(of: L("Claude accounts", "Аккаунты Claude")) ?? -1
        check(claudeHeader >= 0 && h > claudeHeader && signIn > h && titles[h - 1] == "---" && titles[signIn - 1] == "---",
              "menu: Codex section between the Claude accounts and “Sign in…”, separated (\(titles.prefix(9)))")
        let rows = h >= 0 ? Array(menu.items[(h + 1)...].prefix { !$0.isSeparatorItem }) : []
        check(rows.map(\.title) == ["Second  · Pro 5x", "First  · Plus", "fourth@example.com", "third@example.com"],
              "menu: one row per Codex account with its plan (\(rows.map(\.title)))")
        check(rows.map { $0.state == .on } == [false, true, false, false] && rows.allSatisfy { $0.action == #selector(AppDelegate.pickCodexAccount(_:)) },
              "menu: ✓ on the active Codex account, every row switches")
        var done = false
        Task { @MainActor in
            await d.switchCodexTo(byId(B))
            check(d.lastAlert == nil, "switch to the managed account: no alert\(d.lastAlert.map { ": " + $0 } ?? "")")
            check(CodexAccounts.identity(authFile: CodexAccounts.authFile)?.id == B, "auth.json now has the target account")
            list = CodexAccounts.discover()
            check(list.first { $0.isActive }?.accountId == B, "the menu now marks the target as active")
            check(!d.busy, "not busy after the switch")
            d.lastAlert = nil
            await d.switchCodexTo(byId(C))
            check(d.lastAlert != nil, "a failing promote shows an alert")
            check(CodexAccounts.identity(authFile: CodexAccounts.authFile)?.id == B, "a failing promote leaves auth.json as it was")
            d.lastAlert = nil
            await d.switchCodexTo(byId(D))
            check(d.lastAlert != nil, "promote reporting success without changing the account shows an alert")
            check(d.lastAlertText?.contains(L("Codex is still signed in to “Second”, as before.", "В Codex по-прежнему аккаунт «Second».")) == true,
                  "the failure alert names the account auth.json really has (\(d.lastAlertText ?? "-"))")
            check(UsageCollector.lastRequest.map { $0 == (B, "codex") } == true, "the refresh after the switch to B: provider codex, uuid = B")
            check(UsageCollector.tickArguments(tick: "t", uuid: B, provider: "codex") == ["t", "--switch", "--provider", "codex"]
                  && UsageCollector.tickArguments(tick: "t", uuid: "u", provider: "claude") == ["t", "--switch", "--account", "u"],
                  "tick.py: --provider codex for Codex, --account for Claude")
            // e-mail-only accounts and a workspace id that differs from the provider id, added now
            var m = (try! JSONSerialization.jsonObject(with: Data(contentsOf: CodexAccounts.managedFile))) as! [String: Any]
            m["accounts"] = (m["accounts"] as! [[String: Any]]) + [
                ["id": "M5", "email": "fifth@example.com"], ["id": "M6", "email": "sixth@example.com"],
                ["id": "M7", "email": "seventh@example.com", "providerAccountID": "P7-provider", "workspaceAccountID": W7.uppercased()]]
            try! JSONSerialization.data(withJSONObject: m).write(to: CodexAccounts.managedFile)
            list = CodexAccounts.discover()
            func byMail(_ e: String) -> CodexAccount { list.first { $0.email == e }! }
            check(byMail("seventh@example.com").accountId == W7, "workspaceAccountID wins over providerAccountID")
            check(byMail("fifth@example.com").accountId == nil && byMail("fifth@example.com").stateUuid == CodexAccounts.emailUuid("fifth@example.com")
                  && CodexAccounts.emailUuid("noid@example.com") == "email-hash:ab3c8025639b",
                  "an account without an id: stateUuid email-hash:<12 hex> (same as codex_accounts.py)")
            d.lastAlert = nil
            UsageCollector.lastRequest = nil
            await d.switchCodexTo(byMail("fifth@example.com"))
            check(d.lastAlert == nil && CodexAccounts.discover().first { $0.isActive }?.email == "fifth@example.com",
                  "e-mail-only target: landed by its e-mail\(d.lastAlert.map { ": " + $0 } ?? "")")
            check(UsageCollector.lastRequest.map { $0 == (CodexAccounts.emailUuid("fifth@example.com"), "codex") } == true,
                  "e-mail-only switch: the refresh carries its email-hash uuid")
            d.lastAlert = nil
            await d.switchCodexTo(byMail("sixth@example.com"))
            check(d.lastAlert != nil && CodexAccounts.identity(authFile: CodexAccounts.authFile)?.id == B,
                  "e-mail-only target whose promote leaves another login: alert, not “landed”")
            check(d.lastAlertText?.contains(L("Codex is now signed in to “Second”.", "Сейчас в Codex аккаунт «Second».")) == true,
                  "that alert names the account Codex has now (\(d.lastAlertText ?? "-"))")
            d.lastAlert = nil
            await d.switchCodexTo(byMail("seventh@example.com"))
            let act = CodexAccounts.discover().filter { $0.isActive }
            check(d.lastAlert == nil && act.count == 1 && act[0].accountId == W7 && act[0].managedId == "M7",
                  "an e-mail-only auth.json merged into the managed workspace id stays active\(d.lastAlert.map { ": " + $0 } ?? "")")
            check(UsageCollector.lastRequest.map { $0 == (W7, "codex") } == true, "the refresh carries the workspace id W7")
            d.lastAlert = nil
            await d.switchCodexTo(byId(A))
            check(d.lastAlert == nil && CodexAccounts.identity(authFile: CodexAccounts.authFile)?.id == A, "and back to the first account by its managed UUID")

            // --- T3 Code relaunch lifecycle & VPN independence during Codex switch ---
            var t3Open = true
            var t3Events: [String] = []
            T3Code.testController = T3Code.TestController(
                isRunning: { t3Open },
                quit: { t3Events.append("quit"); t3Open = false; return true },
                launch: { t3Events.append("launch"); t3Open = true; return 42424 }
            )
            defer { T3Code.testController = nil }
            d.lastAlert = nil
            d.managedT3LaunchPids.removeAll()
            await d.switchCodexTo(byId(B))
            check(d.lastAlert == nil && t3Events == ["quit", "launch"] && d.managedT3LaunchPids.contains(42424),
                  "codex switch with T3 open: quits T3, promotes, relaunches and tracks managed PID (\(t3Events), pids=\(d.managedT3LaunchPids))")

            // --- Shared-email workspace guard: e-mail-only auth.json must NOT land on an ambiguous workspace ---
            let W8 = "88888888-0000-4000-8000-000000000088", W9 = "99999999-0000-4000-8000-000000000099"
            writeAuth(root.appendingPathComponent("promote/M8.json"), nil, "shared@example.com", "plus")
            var m2 = (try! JSONSerialization.jsonObject(with: Data(contentsOf: CodexAccounts.managedFile))) as! [String: Any]
            m2["accounts"] = (m2["accounts"] as! [[String: Any]]) + [
                ["id": "M8", "email": "shared@example.com", "workspaceAccountID": W8],
                ["id": "M9", "email": "shared@example.com", "workspaceAccountID": W9]]
            try! JSONSerialization.data(withJSONObject: m2).write(to: CodexAccounts.managedFile)
            list = CodexAccounts.discover()
            d.lastAlert = nil
            t3Events.removeAll()
            await d.switchCodexTo(list.first { $0.accountId == W8 }!)
            check(d.lastAlert != nil && t3Events == ["quit", "launch"],
                  "shared-email workspace with id-less auth.json: rejected as ambiguous, T3 still relaunched")

            // Restore clean account A before finishing codex-selftest checks
            T3Code.testController = nil
            d.lastAlert = nil
            await d.switchCodexTo(byId(A))

            // --- AppLaunch imposter classification, planning, and retry loop regression tests ---
            let uid: uid_t = 501
            let t3App = "/Applications/T3 Code (Beta).app"
            let t3MainExe = "/Applications/T3 Code (Beta).app/Contents/MacOS/T3 Code (Beta)"
            let orphanHeadless = AppLaunch.InstanceInfo(
                pid: 98611, bundleID: t3BundleID,
                executablePath: "/Users/test/Library/Caches/ms-playwright/chromium_headless_shell-1248/chrome-headless-shell-mac-arm64/chrome-headless-shell",
                bundlePath: t3App, mainExecutablePath: t3MainExe, isTerminated: false,
                activationPolicy: .regular, ppid: 1, uid: uid,
                commandLine: ".../chrome-headless-shell --remote-debugging-port=0 --no-sandbox http://127.0.0.1:56667/",
                visibleWindowCount: 0)
            let liveHeadless = AppLaunch.InstanceInfo(
                pid: 98612, bundleID: t3BundleID, executablePath: orphanHeadless.executablePath,
                bundlePath: t3App, mainExecutablePath: t3MainExe, isTerminated: false,
                activationPolicy: .regular, ppid: 5555, uid: uid, commandLine: orphanHeadless.commandLine, visibleWindowCount: 0)
            let windowedHeadless = AppLaunch.InstanceInfo(
                pid: 98613, bundleID: t3BundleID, executablePath: orphanHeadless.executablePath,
                bundlePath: t3App, mainExecutablePath: t3MainExe, isTerminated: false,
                activationPolicy: .regular, ppid: 1, uid: uid, commandLine: orphanHeadless.commandLine, visibleWindowCount: 1)
            let helperInsideBundle = AppLaunch.InstanceInfo(
                pid: 98614, bundleID: t3BundleID,
                executablePath: "/Applications/T3 Code (Beta).app/Contents/Frameworks/T3 Code Helper.app/Contents/MacOS/chrome-headless-shell",
                bundlePath: t3App, mainExecutablePath: t3MainExe, isTerminated: false,
                activationPolicy: .regular, ppid: 1, uid: uid, commandLine: "--headless", visibleWindowCount: 0)
            let claudeOsascript = AppLaunch.InstanceInfo(
                pid: 98615, bundleID: claudeBundleID, executablePath: "/usr/bin/osascript",
                bundlePath: "/Applications/Claude.app", mainExecutablePath: "/Applications/Claude.app/Contents/MacOS/Claude",
                isTerminated: false, activationPolicy: .regular, ppid: 1, uid: uid, commandLine: "osascript -e beep", visibleWindowCount: 0)
            let t3BackendNode = AppLaunch.InstanceInfo(
                pid: 98616, bundleID: t3BundleID, executablePath: t3MainExe,
                bundlePath: t3App, mainExecutablePath: t3MainExe, isTerminated: false,
                activationPolicy: .regular, ppid: 1, uid: uid,
                commandLine: "\(t3MainExe) /Applications/T3 Code (Beta).app/Contents/Resources/app.asar/apps/server/dist/bin.mjs",
                visibleWindowCount: 0)
            let t3MainGui = AppLaunch.InstanceInfo(
                pid: 77777, bundleID: t3BundleID, executablePath: t3MainExe,
                bundlePath: t3App, mainExecutablePath: t3MainExe, isTerminated: false,
                activationPolicy: .regular, ppid: 1, uid: uid, commandLine: t3MainExe, visibleWindowCount: 1)

            check(AppLaunch.classify(orphanHeadless, expectedBundleID: t3BundleID, currentUID: uid, excludedCmdSubstrings: T3Code.excludedCmdSubstrings) == .reapableOrphanHeadless,
                  "classify: orphaned Playwright chrome-headless-shell (ppid=1, 0 windows) -> reapableOrphanHeadless")
            check(AppLaunch.classify(liveHeadless, expectedBundleID: t3BundleID, currentUID: uid, excludedCmdSubstrings: T3Code.excludedCmdSubstrings) == .imposter,
                  "classify: live headless browser (ppid!=1) -> imposter (never killed)")
            check(AppLaunch.classify(windowedHeadless, expectedBundleID: t3BundleID, currentUID: uid, excludedCmdSubstrings: T3Code.excludedCmdSubstrings) == .imposter,
                  "classify: browser with visible window -> imposter (never killed)")
            check(AppLaunch.classify(helperInsideBundle, expectedBundleID: t3BundleID, currentUID: uid, excludedCmdSubstrings: T3Code.excludedCmdSubstrings) == .imposter,
                  "classify: binary inside .app bundle -> imposter (never killed)")
            check(AppLaunch.classify(claudeOsascript, expectedBundleID: claudeBundleID, currentUID: uid) == .imposter,
                  "classify: osascript under Claude bundleID -> imposter (never killed)")
            check(AppLaunch.classify(t3BackendNode, expectedBundleID: t3BundleID, currentUID: uid, excludedCmdSubstrings: T3Code.excludedCmdSubstrings) == .imposter,
                  "classify: T3 backend bin.mjs sharing main binary -> NOT main")
            check(AppLaunch.classify(t3MainGui, expectedBundleID: t3BundleID, currentUID: uid, excludedCmdSubstrings: T3Code.excludedCmdSubstrings) == .main,
                  "classify: real T3 Code GUI -> main")

            check(AppLaunch.planLaunch(instances: [orphanHeadless, t3MainGui], expectedBundleID: t3BundleID, currentUID: uid, excludedCmdSubstrings: T3Code.excludedCmdSubstrings) == .activateExisting(pid: 77777),
                  "planLaunch: real main alongside imposter -> activate existing, no duplicate")
            check(AppLaunch.planLaunch(instances: [], expectedBundleID: t3BundleID, currentUID: uid, excludedCmdSubstrings: T3Code.excludedCmdSubstrings) == .launch(newInstance: false, reapPids: [], imposterPids: []),
                  "planLaunch: empty -> normal launch (newInstance=false)")
            check(AppLaunch.planLaunch(instances: [liveHeadless], expectedBundleID: t3BundleID, currentUID: uid, excludedCmdSubstrings: T3Code.excludedCmdSubstrings) == .launch(newInstance: true, reapPids: [], imposterPids: [98612]),
                  "planLaunch: non-reapable imposter -> newInstance=true, reapPids=[]")
            check(AppLaunch.planLaunch(instances: [orphanHeadless], expectedBundleID: t3BundleID, currentUID: uid, excludedCmdSubstrings: T3Code.excludedCmdSubstrings) == .launch(newInstance: false, reapPids: [98611], imposterPids: []),
                  "planLaunch: reapable orphan -> scheduled for reap")

            // Retry loop test 1: orphan reaped cleanly -> normal launch succeeds with real PID
            var simList = [orphanHeadless]
            var openFlags: [Bool] = []
            var reapedPids: [pid_t] = []
            let pid1 = await AppLaunch.runLaunchLoop(
                label: "T3 Code", bundleID: t3BundleID, currentUID: uid,
                excludedCmdSubstrings: T3Code.excludedCmdSubstrings, settleNanos: 0,
                hooks: AppLaunch.Hooks(
                    snapshot: { simList },
                    activate: { _ in },
                    reap: { inst in reapedPids.append(inst.pid); simList.removeAll { $0.pid == inst.pid }; return true },
                    open: { newInst in openFlags.append(newInst); simList.append(t3MainGui); return t3MainGui.pid },
                    openFallback: { _ in },
                    sleep: { _ in }
                ))
            check(pid1 == 77777 && reapedPids == [98611] && openFlags == [false],
                  "runLaunchLoop: reaps orphan, launches normally, returns real PID")

            // Retry loop test 2: non-reapable imposter present -> uses newInstance=true, never kills imposter
            simList = [liveHeadless]; openFlags = []; reapedPids = []
            let pid2 = await AppLaunch.runLaunchLoop(
                label: "T3 Code", bundleID: t3BundleID, currentUID: uid,
                excludedCmdSubstrings: T3Code.excludedCmdSubstrings, settleNanos: 0,
                hooks: AppLaunch.Hooks(
                    snapshot: { simList },
                    activate: { _ in },
                    reap: { inst in reapedPids.append(inst.pid); return false },
                    open: { newInst in openFlags.append(newInst); simList.append(t3MainGui); return t3MainGui.pid },
                    openFallback: { _ in },
                    sleep: { _ in }
                ))
            check(pid2 == 77777 && reapedPids.isEmpty && openFlags == [true],
                  "runLaunchLoop: non-reapable imposter -> bypasses with newInstance=true, zero kills")

            // Retry loop test 3: attempt 1 returns imposter PID (reproducing the exact bug!) -> rejected & retried with newInstance=true
            simList = []; openFlags = []
            var openCall = 0
            let pid3 = await AppLaunch.runLaunchLoop(
                label: "T3 Code", bundleID: t3BundleID, currentUID: uid,
                excludedCmdSubstrings: T3Code.excludedCmdSubstrings, settleNanos: 0,
                hooks: AppLaunch.Hooks(
                    snapshot: { simList },
                    activate: { _ in },
                    reap: { _ in false },
                    open: { newInst in
                        openCall += 1; openFlags.append(newInst)
                        if openCall == 1 { simList = [liveHeadless]; return liveHeadless.pid }
                        simList.append(t3MainGui); return t3MainGui.pid
                    },
                    openFallback: { _ in },
                    sleep: { _ in }
                ))
            check(pid3 == 77777 && openFlags == [false, true],
                  "runLaunchLoop: imposter returned on attempt 1 -> rejected, retried with newInstance=true")

            done = true
        }
        while !done { RunLoop.main.run(until: Date().addingTimeInterval(0.05)) }
    }
    let log = (try? String(contentsOf: Log.file, encoding: .utf8)) ?? ""
    check(log.contains("codex switch: promote rc=3") && log.contains("ready"), "switches are in the log")
    check(!log.contains("never-read") && !log.contains("eyJ"), "no token in the log")
    try? fm.removeItem(at: CodexAccounts.authFile); try? fm.removeItem(at: CodexAccounts.managedFile)
    check(CodexAccounts.discover().isEmpty, "no Codex account: empty list")
    MainActor.assumeIsolated {
        let menu = NSMenu()
        AppDelegate().menuNeedsUpdate(menu)
        check(!menu.items.contains { $0.title == L("Codex accounts", "Аккаунты Codex") } && !menu.items.contains { $0.action == #selector(AppDelegate.pickCodexAccount(_:)) },
              "no Codex account: the menu has no Codex section")
        let t = menu.items.map { $0.isSeparatorItem ? "---" : $0.title }
        check(!zip(t, t.dropFirst()).contains { $0 == "---" && $1 == "---" }, "no Codex account: no doubled separator")
    }
    print(failures == 0 ? "RESULT: all passed, no failures" : "RESULT: \(failures) failures")
    exit(failures == 0 ? 0 : 1)
}

if args.count >= 2, ["list", "add-current", "restore", "set-happ", "happ-test", "cli-status", "cli-save", "cli-login", "launch-t3"].contains(args[1]) {
    let p = Profiles()
    do {
        switch args[1] {
        case "launch-t3":
            var ok = false
            var done = false
            MainActor.assumeIsolated {
                Task { @MainActor in
                    ok = (await T3Code.launchAndWait()) != nil
                    done = true
                }
                while !done { RunLoop.main.run(until: Date().addingTimeInterval(0.05)) }
            }
            exit(ok ? 0 : 1)
        case "list":
            print("current desktop uuid:", p.currentUuid() ?? "-")
            print("current cli uuid:", ClaudeCLI.currentUuid() ?? "-", ClaudeCLI.currentEmail() ?? "")
            for a in p.store.accounts {
                print(a.id == p.store.activeId ? "*" : " ", a.name, a.accountUuid ?? "-",
                      p.hasCliLogin(a) ? "[CLI ok]" : "[CLI missing]",
                      a.useHapp == true ? "VPN Happ" : "")
            }
        case "cli-status":
            print("CLI uuid:", ClaudeCLI.currentUuid() ?? "-", "| email:", ClaudeCLI.currentEmail() ?? "-")
            print("T3 Code running:", T3Code.realRunning != nil)
            let runs = T3Code.activeRuns()
            print("T3 Code active runs:", runs.count)
            for r in runs { print("  -", r.title.isEmpty ? r.threadId : r.title, "(\(r.provider), \(r.status))") }
            for a in p.store.accounts {
                print(" ", a.name, "-> CLI saved:", p.hasCliLogin(a))
            }
        case "cli-save":
            if let a = p.syncLiveCliToMatchingProfile() {
                print("saved CLI login for:", a.name, a.accountUuid ?? "-")
            } else {
                print("no matching account for CLI uuid:", ClaudeCLI.currentUuid() ?? "-")
                exit(1)
            }
        case "cli-login":
            guard args.count >= 3, let a = p.store.accounts.first(where: { $0.name == args[2] }) else { print("usage: cli-login <name>"); exit(2) }
            guard ClaudeCLI.launchTerminalLogin(for: a, relaunchT3: false) else { print("failed to open Terminal"); exit(1) }
            print("opened Terminal login for:", a.name)
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
            if let a = try p.saveCurrent(name: args[2]) {
                _ = p.syncLiveCliToMatchingProfile()
                print("saved:", a.name, a.accountUuid ?? "-")
            } else { print("no one is signed in to Claude"); exit(1) }
        case "restore":
            guard args.count >= 3, let a = p.store.accounts.first(where: { $0.name == args[2] }) else { print("no such account"); exit(2) }
            if (Claude.running != nil || T3Code.running != nil) && ProcessInfo.processInfo.environment["CLAUDE_SWITCHER_CLAUDE_DIR"] == nil {
                print("quit Claude and T3 Code first"); exit(1)
            }
            try p.initialBackupIfNeeded()
            p.cliBackupIfNeeded()
            try p.restore(a)
            let cliOk = try ClaudeCLI.restore(account: a, profileDir: p.profileDir(a))
            p.store.activeId = a.id; p.save()
            print("restored:", a.name, cliOk ? "(desktop + CLI)" : "(desktop; CLI cleared)")
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
