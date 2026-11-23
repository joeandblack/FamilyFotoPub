// FamilyFoto Setup: the small Mac launcher for the shared installer (installer/common/README.md).
//
// It needs no Python, Homebrew or Command Line Tools. It checks the Mac, downloads the pinned
// bootstrap Python and installer engine (verifying their SHA-256 from the embedded
// bootstrap.json), shows the combined agreement before anything else is downloaded, asks for a
// new library or a restore from a backup, then runs installer/macos/online_setup.py and shows
// its progress. That hands over to the release's prebuilt setup window, already set for the
// choice made here. It runs as the signed-in user; no step asks for administrator rights.
import AppKit
import CryptoKit
import Foundation
import SwiftUI

struct Payload: Decodable { let url: String; let sha256: String; let bundle_path: String }

struct Descriptor: Decodable {
    let schema_version: Int
    let mode: String
    let manifest_sha256: String
    let release_id: String
    let minimum_os: String
    let bootstrap_notices: String
    let python: Payload
    let engine: Payload
}

struct LauncherError: LocalizedError {
    let text: String
    init(_ text: String) { self.text = text }
    var errorDescription: String? { text }
}

/// Everything that runs before the user's choices: no UI, so it can be tested and rehearsed.
enum Bootstrap {
    static func isDigest(_ value: String) -> Bool {
        value.count == 64 && value.allSatisfy { "0123456789abcdef".contains($0) }
    }

    static func safeRelative(_ value: String) -> Bool {
        !value.isEmpty && !value.hasPrefix("/") && !value.contains("\\") && !value.contains(":")
            && !value.split(separator: "/", omittingEmptySubsequences: false).contains { $0.isEmpty || $0 == "." || $0 == ".." }
    }

    static func validate(_ d: Descriptor) throws {
        guard d.schema_version == 1, ["online", "offline"].contains(d.mode) else { throw LauncherError("Unknown setup descriptor.") }
        guard isDigest(d.manifest_sha256), version(d.minimum_os) != nil, !d.bootstrap_notices.isEmpty,
              d.release_id.range(of: "^[A-Za-z0-9._-]+$", options: .regularExpression) != nil else {
            throw LauncherError("Invalid setup descriptor.")
        }
        for p in [d.python, d.engine] where !(isDigest(p.sha256) && p.url.hasPrefix("https://") && safeRelative(p.bundle_path)) {
            throw LauncherError("Invalid bootstrap payload in the setup descriptor.")
        }
    }

    static func load(_ url: URL) throws -> Descriptor {
        let d = try JSONDecoder().decode(Descriptor.self, from: Data(contentsOf: url))
        try validate(d)
        return d
    }

    /// One to four numeric components, zero padded, as installer/common/manifest.py compares them.
    static func version(_ text: String) -> [Int]? {
        let parts = text.split(separator: ".", omittingEmptySubsequences: false).map { Int($0) }
        guard (1...4).contains(parts.count), parts.allSatisfy({ $0 != nil && $0! >= 0 }) else { return nil }
        return parts.map { $0! } + Array(repeating: 0, count: 4 - parts.count)
    }

    static func atLeast(_ actual: String, _ minimum: String) -> Bool {
        guard let a = version(actual), let m = version(minimum) else { return false }
        return a.lexicographicallyPrecedes(m) == false
    }

    static var osVersion: String {
        let v = ProcessInfo.processInfo.operatingSystemVersion
        return "\(v.majorVersion).\(v.minorVersion).\(v.patchVersion)"
    }

    static func preflight(_ d: Descriptor, os: String = osVersion, root: Bool = geteuid() == 0) throws {
        #if !arch(arm64)
        throw LauncherError("FamilyFoto needs a Mac with Apple silicon.")
        #endif
        guard !root else { throw LauncherError("Open FamilyFoto Setup as yourself, not as root.") }
        guard atLeast(os, d.minimum_os) else {
            throw LauncherError("FamilyFoto needs macOS \(d.minimum_os) or newer. This Mac has macOS \(os).")
        }
    }

    static func sha256(of url: URL) throws -> String {
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        var hash = SHA256()
        while let block = try handle.read(upToCount: 1 << 20), !block.isEmpty { hash.update(data: block) }
        return hash.finalize().map { String(format: "%02x", $0) }.joined()
    }

    static func base(_ d: Descriptor) -> URL {
        FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("FamilyFoto/Setup/\(d.manifest_sha256)", isDirectory: true)
    }

    /// A verified local copy: cached, downloaded (online) or copied from the app (offline).
    static func fetch(_ p: Payload, as name: String, into base: URL, mode: String, bundle: URL?) throws -> URL {
        let fm = FileManager.default
        try fm.createDirectory(at: base, withIntermediateDirectories: true)
        let target = base.appendingPathComponent(name)
        if fm.fileExists(atPath: target.path) {
            if try sha256(of: target) == p.sha256 { return target }
            throw LauncherError("A cached setup file is damaged: \(target.path). Remove it and open Setup again.")
        }
        let partial = base.appendingPathComponent(".\(name).\(UUID().uuidString).partial")
        defer { try? fm.removeItem(at: partial) }
        if mode == "offline" {
            guard let bundle else { throw LauncherError("This offline package is incomplete.") }
            try copyBytes(bundle.appendingPathComponent(p.bundle_path), to: partial)
        } else {
            try download(URL(string: p.url)!, to: partial)
        }
        guard try sha256(of: partial) == p.sha256 else { throw LauncherError("A downloaded setup file failed its checksum. Nothing was installed.") }
        try fm.moveItem(at: partial, to: target)
        return target
    }

    /// Contents only: no extended attributes, so a quarantined disk image's mark is not carried
    /// onto the bootstrap Python (FileManager.copyItem would copy com.apple.quarantine).
    static func copyBytes(_ source: URL, to destination: URL) throws {
        let input = try FileHandle(forReadingFrom: source)
        defer { try? input.close() }
        guard FileManager.default.createFile(atPath: destination.path, contents: nil) else {
            throw LauncherError("Could not write \(destination.lastPathComponent).")
        }
        let output = try FileHandle(forWritingTo: destination)
        defer { try? output.close() }
        while let block = try input.read(upToCount: 1 << 20), !block.isEmpty { try output.write(contentsOf: block) }
    }

    static func download(_ url: URL, to destination: URL) throws {
        // Files this app downloads itself carry no quarantine, unlike the browser download of the app.
        let done = DispatchSemaphore(value: 0)
        var failure: Error?
        let task = URLSession.shared.downloadTask(with: url) { file, response, error in
            defer { done.signal() }
            if let error { failure = error; return }
            guard let file, (response as? HTTPURLResponse)?.statusCode == 200 else {
                failure = LauncherError("Download failed: \(url.absoluteString)"); return
            }
            do { try FileManager.default.moveItem(at: file, to: destination) } catch { failure = error }
        }
        task.resume()
        done.wait()
        if let failure { throw failure }
    }

    /// Names and Unix modes from a ZIP's central directory, read without extracting anything.
    static func zipEntries(_ archive: URL) throws -> [(name: String, mode: UInt32)] {
        let data = try Data(contentsOf: archive, options: .mappedIfSafe)
        func u16(_ at: Int) -> Int { Int(data[at]) | Int(data[at + 1]) << 8 }
        func u32(_ at: Int) -> Int { u16(at) | u16(at + 2) << 16 }
        let unreadable = LauncherError("A setup file is not a readable archive.")
        // End of central directory: signature 0x06054b50 within the last 64 KiB + 22 bytes.
        guard data.count >= 22 else { throw unreadable }
        var end = data.count - 22
        while end >= max(0, data.count - 65_557), u32(end) != 0x0605_4b50 { end -= 1 }
        guard end >= 0, u32(end) == 0x0605_4b50 else { throw unreadable }
        let count = u16(end + 10), offset = u32(end + 16)
        guard count != 0xFFFF, offset != 0xFFFF_FFFF else { throw LauncherError("Large (ZIP64) setup archives are not supported.") }
        var at = offset, entries: [(String, UInt32)] = []
        for _ in 0..<count {
            guard at + 46 <= data.count, u32(at) == 0x0201_4b50 else { throw unreadable }
            let nameLength = u16(at + 28), extra = u16(at + 30), comment = u16(at + 32)
            guard at + 46 + nameLength <= data.count,
                  let name = String(data: data[(at + 46)..<(at + 46 + nameLength)], encoding: .utf8) else { throw unreadable }
            entries.append((name, UInt32(u32(at + 38)) >> 16))
            at += 46 + nameLength + extra + comment
        }
        return entries
    }

    /// Before anything is written: only plain files and folders at safe relative paths.
    static func checkArchive(_ archive: URL) throws {
        for (name, mode) in try zipEntries(archive) {
            let path = name.hasSuffix("/") ? String(name.dropLast()) : name
            let kind = mode & 0o170000
            guard safeRelative(path), kind == 0 || kind == 0o100000 || kind == 0o040000 else {
                throw LauncherError("A setup file contains an unsafe entry: \(name)")
            }
        }
    }

    /// Release payloads are link-free ZIPs (installer/common/README.md); ditto keeps executable bits.
    static func extract(_ archive: URL, to destination: URL) throws {
        let fm = FileManager.default
        guard !fm.fileExists(atPath: destination.path) else { throw LauncherError("Unexpected setup folder: \(destination.path)") }
        try checkArchive(archive)  // links or `..` could otherwise write outside the session folder
        let ditto = Process()
        ditto.executableURL = URL(fileURLWithPath: "/usr/bin/ditto")
        // --noqtn: never propagate a quarantine mark onto the unpacked bootstrap programs.
        ditto.arguments = ["-x", "-k", "--noqtn", archive.path, destination.path]
        try ditto.run()
        ditto.waitUntilExit()
        guard ditto.terminationStatus == 0 else { throw LauncherError("A setup file could not be unpacked.") }
        try refuseLinks(in: destination)
    }

    static func refuseLinks(in folder: URL) throws {
        let keys: [URLResourceKey] = [.isSymbolicLinkKey]
        guard let walk = FileManager.default.enumerator(at: folder, includingPropertiesForKeys: keys) else { return }
        for case let url as URL in walk where try url.resourceValues(forKeys: Set(keys)).isSymbolicLink == true {
            throw LauncherError("A setup file contains an unexpected link: \(url.lastPathComponent)")
        }
    }

    /// Why a folder cannot be restored from, or nil. online_setup.py checks it again.
    static func backupProblem(_ backup: String, installFolder: String) -> String? {
        guard !backup.isEmpty else { return "Choose the backup folder." }
        let fm = FileManager.default
        var isFolder: ObjCBool = false
        let path = URL(fileURLWithPath: backup).standardizedFileURL.path
        guard fm.fileExists(atPath: path + "/catalog", isDirectory: &isFolder), isFolder.boolValue else {
            return "This is not a FamilyFoto backup folder: choose the folder that holds catalog and restore-on-mac."
        }
        let target = URL(fileURLWithPath: installFolder).standardizedFileURL.path
        if target == path || target.hasPrefix(path + "/") || path.hasPrefix(target + "/") {
            return "Choose an installation folder outside the backup folder."
        }
        return nil
    }

    struct Agreement { let text: String; let digest: String }

    /// The engine's pinned manifest names the agreement; both are checked before it is shown.
    static func agreement(engine: URL, pinned: String) throws -> Agreement {
        let manifestURL = engine.appendingPathComponent("installer-manifest.json")
        guard try sha256(of: manifestURL) == pinned,
              let doc = try JSONSerialization.jsonObject(with: Data(contentsOf: manifestURL)) as? [String: Any],
              let path = doc["agreement_path"] as? String, safeRelative(path),
              let expected = doc["agreement_sha256"] as? String else {
            throw LauncherError("The installer engine does not match this setup app.")
        }
        let url = engine.appendingPathComponent(path)
        let digest = try sha256(of: url)
        guard digest == expected, let text = String(data: try Data(contentsOf: url), encoding: .utf8) else {
            throw LauncherError("The agreement does not match this release.")
        }
        return Agreement(text: text, digest: digest)
    }

    struct Prepared { let python: URL; let engine: URL; let agreement: Agreement; let base: URL }

    /// Earlier launches' unpacked bootstrap folders; only `session-<UUID>` folders this app made.
    static func removeOldSessions(in base: URL) {
        let fm = FileManager.default
        for name in (try? fm.contentsOfDirectory(atPath: base.path)) ?? []
        where name.hasPrefix("session-") && UUID(uuidString: String(name.dropFirst(8))) != nil {
            try? fm.removeItem(at: base.appendingPathComponent(name, isDirectory: true))
        }
    }

    static func prepare(_ d: Descriptor, bundle: URL?) throws -> Prepared {
        try preflight(d)
        let base = base(d)
        let python = try fetch(d.python, as: "python.zip", into: base, mode: d.mode, bundle: bundle)
        let engine = try fetch(d.engine, as: "engine.zip", into: base, mode: d.mode, bundle: bundle)
        // A fresh session folder each launch: never trust an earlier, possibly altered, extraction.
        removeOldSessions(in: base)
        let session = base.appendingPathComponent("session-\(UUID().uuidString)", isDirectory: true)
        try extract(python, to: session.appendingPathComponent("python"))
        try extract(engine, to: session.appendingPathComponent("engine"))
        let interpreter = session.appendingPathComponent("python/bin/python3.12")
        guard FileManager.default.isExecutableFile(atPath: interpreter.path) else { throw LauncherError("The bootstrap Python is incomplete.") }
        let engineDir = session.appendingPathComponent("engine")
        return Prepared(python: interpreter, engine: engineDir, agreement: try agreement(engine: engineDir, pinned: d.manifest_sha256), base: base)
    }

    /// Headless rehearsal: everything up to the agreement, then exit. `FamilyFoto Setup --check`.
    static func check(descriptor: URL, bundle: URL?) -> Int32 {
        do {
            let d = try load(descriptor)
            let p = try prepare(d, bundle: bundle)
            print("ok: macOS \(osVersion), release \(d.release_id), agreement \(p.agreement.digest)")
            print("bootstrap python: \(p.python.path)")
            try? FileManager.default.removeItem(at: p.engine.deletingLastPathComponent())
            return 0
        } catch {
            FileHandle.standardError.write(Data(("Setup check failed: \(error.localizedDescription)\n").utf8))
            return 1
        }
    }

    /// Progress text and fraction for one `@@ {json}` line (installer/common/README.md, "Events").
    static func progress(_ line: String) -> (String, Double?)? {
        guard line.hasPrefix("@@ "), let event = try? JSONSerialization.jsonObject(with: Data(line.dropFirst(3).utf8)) as? [String: Any],
              event["schema_version"] as? Int == 1 else { return nil }
        let component = event["component_id"] as? String ?? "components"
        switch (event["stage"] as? String, event["state"] as? String) {
        case ("acquire", "completed"): return ("Downloaded \(component)", nil)
        case ("acquire", _):
            if event["unit"] as? String == "bytes", let n = (event["completed"] as? NSNumber)?.doubleValue,
               let t = (event["total"] as? NSNumber)?.doubleValue, t > 0 {
                return (String(format: "Downloading %@ · %.1f of %.1f MB", component, n / 1e6, t / 1e6), min(1, n / t))
            }
            return ("Downloading \(component)", nil)
        case ("prepare", "completed"): return ("Downloads verified", nil)
        default: return ("Working", nil)
        }
    }
}

enum Phase: Equatable { case preparing, choosing, installing, handedOff, finished, failed }

@MainActor
final class Launcher: ObservableObject {
    @Published var phase = Phase.preparing
    @Published var status = "Checking this Mac…"
    @Published var fraction: Double?
    @Published var accepted = false
    @Published var people = false
    @Published var restore = false
    @Published var backup = ""
    @Published var folder = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("FamilyFoto").path
    @Published var log = ""
    var descriptor: Descriptor?
    var prepared: Bootstrap.Prepared?
    var agreementText: String { prepared?.agreement.text ?? "" }
    var notices: String { descriptor?.bootstrap_notices ?? "" }
    private var process: Process?
    private let resources = Bundle.main.resourceURL!

    var backupProblem: String? { restore ? Bootstrap.backupProblem(backup, installFolder: folder) : nil }

    var bundle: URL? { descriptor?.mode == "offline" ? resources.appendingPathComponent("bundle") : nil }
    var attempt: URL? { prepared.map { $0.base.appendingPathComponent("attempt-\(descriptor!.mode)") } }

    func start() async {
        let descriptorURL = resources.appendingPathComponent("bootstrap.json")
        do {
            let d = try Bootstrap.load(descriptorURL)
            descriptor = d
            status = "Preparing FamilyFoto Setup…"
            let bundle = self.bundle
            prepared = try await Task.detached { try Bootstrap.prepare(d, bundle: bundle) }.value
            phase = .choosing
        } catch { fail(error.localizedDescription) }
    }

    func install() {
        guard let p = prepared, let attempt, accepted else { return }
        do {
            try FileManager.default.createDirectory(at: attempt, withIntermediateDirectories: true)
            var choices: [String: Any] = ["root": URL(fileURLWithPath: folder).standardizedFileURL.path,
                                          "people": people, "agreement_digest": p.agreement.digest]
            if restore {
                guard backupProblem == nil else { return }
                choices["backup"] = URL(fileURLWithPath: backup).standardizedFileURL.path
            }
            let choicesURL = attempt.appendingPathComponent("choices.json")
            try JSONSerialization.data(withJSONObject: choices).write(to: choicesURL, options: .atomic)
            let run = Process()
            run.executableURL = p.python
            run.currentDirectoryURL = p.engine
            run.arguments = ["-E", "-s", "-B", "-m", "installer.macos.online_setup",
                             "--descriptor", resources.appendingPathComponent("bootstrap.json").path,
                             "--attempt", attempt.path, "--choices", choicesURL.path,
                             "--os-version", Bootstrap.osVersion] + (bundle.map { ["--bundle", $0.path] } ?? [])
            let pipe = Pipe()
            run.standardOutput = pipe
            run.standardError = pipe
            pipe.fileHandleForReading.readabilityHandler = { handle in
                let text = String(decoding: handle.availableData, as: UTF8.self)
                Task { @MainActor in self.consume(text) }
            }
            run.terminationHandler = { finished in
                Task { @MainActor in self.ended(finished.terminationStatus) }
            }
            phase = .installing
            status = "Downloading FamilyFoto…"
            try run.run()
            process = run
        } catch { fail(error.localizedDescription) }
    }

    func consume(_ text: String) {
        for line in text.split(separator: "\n").map(String.init) {
            if let (message, value) = Bootstrap.progress(line) {
                status = message; fraction = value
            } else if !line.isEmpty {
                log += line + "\n"
                if line.hasPrefix("Downloads verified.") { phase = .handedOff; status = "Continue in the FamilyFoto Setup window."; fraction = nil }
            }
        }
    }

    func ended(_ code: Int32) {
        process = nil
        if code == 0 { phase = .finished; status = "FamilyFoto Setup has finished." }
        else { fail(log.split(separator: "\n").last.map(String.init) ?? "Setup stopped (exit \(code)).") }
    }

    func cancel() {
        guard let attempt else { return }
        FileManager.default.createFile(atPath: attempt.appendingPathComponent("cancel.requested").path, contents: Data())
        status = "Stopping after the current download… Verified downloads are kept."
    }

    func fail(_ message: String) { phase = .failed; status = message; fraction = nil }

    func chooseBackup() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.prompt = "Choose"
        panel.message = "Choose the FamilyFoto backup folder: the folder that holds catalog and restore-on-mac."
        if panel.runModal() == .OK, let url = panel.url { backup = url.path }
    }

    func chooseFolder() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.canCreateDirectories = true
        panel.prompt = "Choose"
        panel.message = "Choose an empty folder for FamilyFoto, or create a new one."
        if panel.runModal() == .OK, let url = panel.url { folder = url.path }
    }
}

struct LauncherView: View {
    @EnvironmentObject var launcher: Launcher
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Set up FamilyFoto").font(.title).bold()
            switch launcher.phase {
            case .choosing: choices
            case .preparing, .installing, .handedOff:
                if let value = launcher.fraction { ProgressView(value: value) } else { ProgressView() }
                Text(launcher.status)
                Spacer()
                if launcher.phase == .installing { HStack { Spacer(); Button("Cancel") { launcher.cancel() } } }
            case .finished, .failed:
                Text(launcher.status).foregroundStyle(launcher.phase == .failed ? .red : .primary)
                if !launcher.log.isEmpty {
                    DisclosureGroup("Details") { ScrollView { Text(launcher.log).font(.caption.monospaced()).textSelection(.enabled) }.frame(height: 160) }
                }
                Spacer()
                HStack { Spacer(); Button("Close") { NSApp.terminate(nil) }.keyboardShortcut(.defaultAction) }
            }
        }
        .padding(24)
    }

    var choices: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("FamilyFoto Terms and Third-Party Notices").font(.headline)
            ScrollView { Text(launcher.agreementText).font(.callout).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading) }
                .frame(height: 180).padding(8).background(Color(nsColor: .textBackgroundColor)).border(Color.secondary.opacity(0.3))
            Toggle("I accept the FamilyFoto terms and the applicable third-party terms listed here for the components I selected.",
                   isOn: $launcher.accepted)
            Picker("Library", selection: $launcher.restore) {
                Text("New library").tag(false); Text("Restore from a backup").tag(true)
            }.pickerStyle(.segmented)
            if launcher.restore {
                HStack {
                    Text("Backup folder")
                    TextField("", text: $launcher.backup)
                    Button("Browse…") { launcher.chooseBackup() }
                }
                if let problem = launcher.backupProblem, !launcher.backup.isEmpty {
                    Text(problem).font(.callout).foregroundStyle(.red)
                } else {
                    Text("Also for a library moved from another computer, Mac or Windows. You choose how to restore the photos and videos, and enter the backup's password, in the next window.")
                        .font(.callout).foregroundStyle(.secondary)
                }
            }
            HStack {
                Text("Installation folder")
                TextField("", text: $launcher.folder)
                Button("Browse…") { launcher.chooseFolder() }
            }
            Toggle("Include People (face recognition; a larger download)", isOn: $launcher.people)
            if launcher.restore && !launcher.people {
                Text("Without People, the backup's saved people stay viewable, but new photos are not scanned.").font(.caption).foregroundStyle(.secondary)
            }
            Text(launcher.notices).font(.caption).foregroundStyle(.secondary)
            Spacer()
            HStack {
                Spacer()
                Button("Quit") { NSApp.terminate(nil) }
                Button(launcher.restore ? "Install and Restore" : "Install") { launcher.install() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(!launcher.accepted || launcher.folder.isEmpty || launcher.backupProblem != nil)
            }
        }
    }
}

struct FamilyFotoSetupApp: App {
    @StateObject private var launcher = Launcher()
    var body: some Scene {
        WindowGroup("FamilyFoto Setup") {
            LauncherView().environmentObject(launcher)
                .frame(width: 640, height: 600)
                .task { await launcher.start() }
        }
        .windowResizability(.contentSize)
    }
}

#if !LAUNCHER_TEST
@main
enum Entry {
    static func main() {
        let arguments = CommandLine.arguments
        if let i = arguments.firstIndex(of: "--check") {
            // Rehearsal: `--check [DESCRIPTOR [BUNDLE]]`; defaults to the app's own resources.
            let resources = Bundle.main.resourceURL!
            let descriptor = arguments.count > i + 1 ? URL(fileURLWithPath: arguments[i + 1]) : resources.appendingPathComponent("bootstrap.json")
            let bundle = arguments.count > i + 2 ? URL(fileURLWithPath: arguments[i + 2]) : resources.appendingPathComponent("bundle")
            exit(Bootstrap.check(descriptor: descriptor, bundle: bundle))
        }
        FamilyFotoSetupApp.main()
    }
}
#endif
