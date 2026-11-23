// claude-limits-helper: the only part of the system that touches the saved login.
//
// What it does: takes the sessionKey cookie from the given Cookies file (a Claude Switcher profile
// or the live Claude folder), decrypts it with the key from the Keychain ("Claude Safe Storage")
// and asks claude.ai for this account's limit percentages, with the same request CodexBar uses.
//
// What it does not do: it writes nothing to the profiles, does not refresh or change the login, and
// prints neither the key nor the cookie. Only JSON with the server's limits answer goes to stdout.
//
// VPN rule: --vpn on|off: the request is sent only if the VPN Happ state right before
// sending matches the required one (determined the same way Claude Switcher does it:
// the default route goes through utun and xray is running).
//
//   claude-limits-helper usage --cookies <Cookies file> --vpn on|off [--org <uuid>]
//   claude-limits-helper vpn          -> {"vpn":true|false,"state":"on"|"off"|"foreign"|"unknown"}
//   claude-limits-helper check        -> checks Keychain access (triggers the system prompt)

import CommonCrypto
import CryptoKit
import Foundation
import Security
import SQLite3

func out(_ obj: [String: Any]) -> Never {
    let data = try! JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys])
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write("\n".data(using: .utf8)!)
    exit(obj["ok"] as? Bool == false ? 1 : 0)
}

func fail(_ status: String, _ detail: String = "") -> Never {
    out(["ok": false, "status": status, "detail": detail])
}

func run(_ path: String, _ args: [String]) -> (Int32, String) {
    let p = Process(); p.executableURL = URL(fileURLWithPath: path); p.arguments = args
    let pipe = Pipe(); p.standardOutput = pipe; p.standardError = FileHandle.nullDevice
    do { try p.run() } catch { return (-1, "") }
    let data = pipe.fileHandleForReading.readDataToEndOfFile()
    p.waitUntilExit()
    return (p.terminationStatus, String(decoding: data, as: UTF8.self))
}

/// "on": the default route goes through the expected VPN (a tunnel interface with xray running).
/// "off": a direct connection. "foreign": a tunnel that is not ours. "unknown": the route could not be read.
/// Limits are requested only in the first two states; a foreign VPN or an unreadable route is never
/// treated as "no VPN", otherwise an account would be queried from an address it must not be seen on.
func vpnState() -> String {
    let (code, route) = run("/sbin/route", ["-n", "get", "default"])
    var iface = ""
    for l in route.split(separator: "\n") where l.contains("interface:") {
        iface = l.split(separator: ":").last.map { $0.trimmingCharacters(in: .whitespaces) } ?? ""
    }
    guard code == 0, !iface.isEmpty else { return "unknown" }
    guard iface.hasPrefix("utun") || iface.hasPrefix("ipsec") || iface.hasPrefix("ppp") else { return "off" }
    return iface.hasPrefix("utun") && run("/usr/bin/pgrep", ["-x", "xray"]).0 == 0 ? "on" : "foreign"
}

func safeStorageKey() -> [UInt8] {
    let q: [String: Any] = [
        kSecClass as String: kSecClassGenericPassword,
        kSecAttrService as String: "Claude Safe Storage",
        kSecReturnData as String: true,
        kSecMatchLimit as String: kSecMatchLimitOne,
    ]
    var item: CFTypeRef?
    let st = SecItemCopyMatching(q as CFDictionary, &item)
    guard st == errSecSuccess, let pw = item as? Data else {
        fail("keychain_denied", "No access to the \"Claude Safe Storage\" Keychain item (code \(st))")
    }
    var key = [UInt8](repeating: 0, count: 16)
    let salt = Array("saltysalt".utf8)
    let rc = pw.withUnsafeBytes { (p: UnsafeRawBufferPointer) in
        CCKeyDerivationPBKDF(CCPBKDFAlgorithm(kCCPBKDF2), p.bindMemory(to: Int8.self).baseAddress, pw.count,
                             salt, salt.count, CCPseudoRandomAlgorithm(kCCPRFHmacAlgSHA1), 1003, &key, 16)
    }
    guard rc == kCCSuccess else { fail("kdf_failed") }
    return key
}

func decrypt(_ blob: Data, key: [UInt8], host: String) -> String? {
    guard blob.count > 3, blob.prefix(3) == Data("v10".utf8) else { return nil }
    let ct = [UInt8](blob.dropFirst(3))
    let iv = [UInt8](repeating: 0x20, count: 16)
    var outBuf = [UInt8](repeating: 0, count: ct.count + 16)
    var n = 0
    let rc = CCCrypt(CCOperation(kCCDecrypt), CCAlgorithm(kCCAlgorithmAES), CCOptions(kCCOptionPKCS7Padding),
                     key, key.count, iv, ct, ct.count, &outBuf, outBuf.count, &n)
    guard rc == kCCSuccess else { return nil }
    var plain = Data(outBuf.prefix(n))
    // newer Chromium versions prepend the SHA-256 of the host name
    let digest = Data(SHA256.hash(data: Data(host.utf8)))
    if plain.count >= 32, plain.prefix(32) == digest { plain = plain.dropFirst(32) }
    return String(data: plain, encoding: .utf8)
}

func readCookies(_ path: String, key: [UInt8]) -> [String: String] {
    var db: OpaquePointer?
    let uri = "file:" + (path.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? path) + "?mode=ro&immutable=1"
    guard sqlite3_open_v2(uri, &db, SQLITE_OPEN_READONLY | SQLITE_OPEN_URI, nil) == SQLITE_OK else {
        fail("cookies_unreadable", path)
    }
    defer { sqlite3_close(db) }
    var st: OpaquePointer?
    let sql = "SELECT host_key, name, encrypted_value FROM cookies WHERE host_key LIKE '%claude.ai' AND name IN ('sessionKey','lastActiveOrg')"
    guard sqlite3_prepare_v2(db, sql, -1, &st, nil) == SQLITE_OK else { fail("cookies_unreadable", "sql") }
    defer { sqlite3_finalize(st) }
    var res: [String: String] = [:]
    while sqlite3_step(st) == SQLITE_ROW {
        let host = String(cString: sqlite3_column_text(st, 0))
        let name = String(cString: sqlite3_column_text(st, 1))
        let n = Int(sqlite3_column_bytes(st, 2))
        guard n > 0, let p = sqlite3_column_blob(st, 2) else { continue }
        if let v = decrypt(Data(bytes: p, count: n), key: key, host: host) { res[name] = v }
    }
    return res
}

func get(_ url: String, sessionKey: String) -> (Int, Any?) {
    var req = URLRequest(url: URL(string: url)!, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 20)
    req.setValue("sessionKey=\(sessionKey)", forHTTPHeaderField: "Cookie")
    req.setValue("application/json", forHTTPHeaderField: "Accept")
    let cfg = URLSessionConfiguration.ephemeral
    cfg.httpShouldSetCookies = false; cfg.httpCookieAcceptPolicy = .never
    let sem = DispatchSemaphore(value: 0)
    var code = 0; var body: Any?
    URLSession(configuration: cfg).dataTask(with: req) { data, resp, _ in
        code = (resp as? HTTPURLResponse)?.statusCode ?? 0
        if let data { body = try? JSONSerialization.jsonObject(with: data) }
        sem.signal()
    }.resume()
    sem.wait()
    return (code, body)
}

let args = CommandLine.arguments
func arg(_ name: String) -> String? {
    guard let i = args.firstIndex(of: name), i + 1 < args.count else { return nil }
    return args[i + 1]
}

switch args.count > 1 ? args[1] : "" {
case "vpn":
    let state = vpnState()
    out(["ok": true, "vpn": state == "on", "state": state])
case "check":
    _ = safeStorageKey()
    out(["ok": true, "status": "keychain_ok"])
case "usage":
    guard let cookies = arg("--cookies"), let want = arg("--vpn"), want == "on" || want == "off" else {
        fail("bad_args", "usage --cookies <file> --vpn on|off [--org <uuid>]")
    }
    guard vpnState() == want else { fail("vpn_mismatch") }
    let key = safeStorageKey()
    let jar = readCookies(cookies, key: key)
    guard let sk = jar["sessionKey"], sk.hasPrefix("sk-ant-") else { fail("no_session_cookie") }
    let uuidChars = CharacterSet(charactersIn: "0123456789abcdef-")
    guard let org = arg("--org") ?? jar["lastActiveOrg"],
          org.count == 36, org.unicodeScalars.allSatisfy({ uuidChars.contains($0) }) else { fail("no_org") }
    guard vpnState() == want else { fail("vpn_mismatch") }  // the VPN may have dropped while we were reading
    let (code, body) = get("https://claude.ai/api/organizations/\(org)/usage", sessionKey: sk)
    guard code == 200, let usage = body as? [String: Any] else {
        fail(code == 401 || code == 403 ? "auth_rejected" : (code == 429 ? "rate_limited" : "http_error"), "HTTP \(code)")
    }
    var res: [String: Any] = ["ok": true, "status": "ok", "org": org, "usage": usage,
                              "fetchedAt": Int(Date().timeIntervalSince1970)]
    if args.contains("--plan"), vpnState() == want {
        let (c2, b2) = get("https://claude.ai/api/organizations/\(org)", sessionKey: sk)
        if c2 == 200, let o = b2 as? [String: Any] {
            var plan: [String: Any] = [:]
            for k in ["rate_limit_tier", "capabilities", "billing_type", "raven_type"] where o[k] != nil { plan[k] = o[k] }
            res["plan"] = plan
        }
    }
    out(res)
default:
    fail("bad_args", "commands: usage | vpn | check")
}
