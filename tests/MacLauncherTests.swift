// Compile alongside installer/macos/launcher/Launcher.swift with -parse-as-library -D LAUNCHER_TEST.
import Foundation

@main
struct LauncherTests {
    static func descriptor(_ change: (inout [String: Any]) -> Void = { _ in }) throws -> Descriptor {
        let digest = String(repeating: "a", count: 64)
        var doc: [String: Any] = [
            "schema_version": 1, "mode": "online", "manifest_sha256": digest, "release_id": "test-1",
            "minimum_os": "27.0", "bootstrap_notices": "notices",
            "python": ["url": "https://example.com/p.zip", "sha256": digest, "bundle_path": "bootstrap/python.zip"],
            "engine": ["url": "https://example.com/e.zip", "sha256": digest, "bundle_path": "bootstrap/engine.zip"]]
        change(&doc)
        let d = try JSONDecoder().decode(Descriptor.self, from: JSONSerialization.data(withJSONObject: doc))
        try Bootstrap.validate(d)
        return d
    }

    static func refused(_ change: @escaping (inout [String: Any]) -> Void) -> Bool {
        (try? descriptor(change)) == nil
    }

    static func main() throws {
        // Versions compare numerically with zero padding, like installer/common/manifest.py.
        assert(Bootstrap.version("14") == [14, 0, 0, 0] && Bootstrap.version("14.2.1") == [14, 2, 1, 0])
        assert(Bootstrap.version("14.beta") == nil && Bootstrap.version("") == nil && Bootstrap.version("1.2.3.4.5") == nil)
        assert(Bootstrap.atLeast("14.0", "14") && Bootstrap.atLeast("27.2.1", "14.0") && Bootstrap.atLeast("14.10", "14.9"))
        assert(!Bootstrap.atLeast("13.6.9", "14.0") && !Bootstrap.atLeast("bad", "14.0"))

        // Descriptor rules mirror installer/macos/build_launcher.py.
        let good = try descriptor()
        assert(good.mode == "online")
        assert(refused { $0["schema_version"] = 2 })
        assert(refused { $0["mode"] = "sideload" })
        assert(refused { $0["manifest_sha256"] = "ABC" })
        assert(refused { $0["minimum_os"] = "fourteen" })
        assert(refused { $0["release_id"] = "a b" })
        assert(refused { $0["python"] = ["url": "http://example.com/p.zip", "sha256": String(repeating: "a", count: 64), "bundle_path": "p.zip"] })
        assert(refused { $0["engine"] = ["url": "https://example.com/e.zip", "sha256": String(repeating: "a", count: 64), "bundle_path": "../e.zip"] })
        for bad in ["", "/abs", "a/../b", "a//b", "C:x", "a\\b", "./a"] { assert(!Bootstrap.safeRelative(bad), bad) }
        assert(Bootstrap.safeRelative("bootstrap/python.zip"))

        // Preflight: an older macOS or root is refused before anything is downloaded.
        do { try Bootstrap.preflight(good, os: "26.4", root: false); assertionFailure("old macOS accepted") }
        catch { assert(error.localizedDescription.contains("macOS 27.0 or newer")) }
        do { try Bootstrap.preflight(good, os: "27.2", root: true); assertionFailure("root accepted") }
        catch { assert(error.localizedDescription.contains("not as root")) }
        try Bootstrap.preflight(good, os: "27.2", root: false)

        // SHA-256 of a file, streamed.
        let file = FileManager.default.temporaryDirectory.appendingPathComponent("launcher-test-\(getpid()).txt")
        try Data("abc".utf8).write(to: file)
        let digest = try Bootstrap.sha256(of: file)
        assert(digest == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
        try FileManager.default.removeItem(at: file)

        // Progress lines: only shared v1 events; byte counts only for the bytes unit.
        let running = Bootstrap.progress("@@ {\"schema_version\": 1, \"stage\": \"acquire\", \"state\": \"running\", \"component_id\": \"python\", \"completed\": 5000000, \"total\": 20000000, \"unit\": \"bytes\"}")
        assert(running?.0 == "Downloading python · 5.0 of 20.0 MB" && running?.1 == 0.25)
        assert(Bootstrap.progress("@@ {\"schema_version\": 1, \"stage\": \"acquire\", \"state\": \"running\", \"completed\": 1, \"total\": 2, \"unit\": \"files\"}")?.1 == nil)
        assert(Bootstrap.progress("@@ {\"schema_version\": 1, \"stage\": \"prepare\", \"state\": \"completed\"}")?.0 == "Downloads verified")
        assert(Bootstrap.progress("@@ {\"stage\": \"restored\", \"state\": \"running\"}") == nil)  // legacy Move event
        assert(Bootstrap.progress("plain output") == nil)

        // Links inside an unpacked bootstrap folder are refused.
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent("launcher-links-\(getpid())")
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        try Bootstrap.refuseLinks(in: folder)
        try FileManager.default.createSymbolicLink(at: folder.appendingPathComponent("escape"), withDestinationURL: URL(fileURLWithPath: "/etc"))
        do { try Bootstrap.refuseLinks(in: folder); assertionFailure("link accepted") } catch {}
        try FileManager.default.removeItem(at: folder)
        // Archive entries are checked before extraction: links and escaping paths are refused.
        func zip(_ name: String, _ build: String) throws -> URL {
            let url = FileManager.default.temporaryDirectory.appendingPathComponent("launcher-\(getpid())-\(name).zip")
            try? FileManager.default.removeItem(at: url)
            let python = Process()
            python.executableURL = URL(fileURLWithPath: "/usr/bin/env")
            python.arguments = ["python3", "-c", "import stat, sys, zipfile\nz = zipfile.ZipFile(sys.argv[1], 'w')\n" + build, url.path]
            try python.run(); python.waitUntilExit()
            assert(python.terminationStatus == 0)
            return url
        }
        let plain = try zip("plain", "i = zipfile.ZipInfo('bin/python3.12'); i.external_attr = (stat.S_IFREG | 0o755) << 16; z.writestr(i, 'x'); z.writestr('lib/', ''); z.close()")
        try Bootstrap.checkArchive(plain)
        let names = try Bootstrap.zipEntries(plain).map(\.name)
        assert(names == ["bin/python3.12", "lib/"], "\(names)")
        let linked = try zip("link", "i = zipfile.ZipInfo('bin/python3'); i.external_attr = (stat.S_IFLNK | 0o777) << 16; z.writestr(i, '/etc'); z.close()")
        let escaping = try zip("escape", "z.writestr('../outside.txt', 'x'); z.close()")
        for bad in [linked, escaping] {
            do { try Bootstrap.checkArchive(bad); assertionFailure("unsafe archive accepted: \(bad.lastPathComponent)") }
            catch { assert(error.localizedDescription.contains("unsafe entry")) }
        }
        let junk = FileManager.default.temporaryDirectory.appendingPathComponent("launcher-\(getpid())-junk.zip")
        try Data("not a zip".utf8).write(to: junk)
        do { try Bootstrap.checkArchive(junk); assertionFailure("junk accepted") } catch {}
        for url in [plain, linked, escaping, junk] { try? FileManager.default.removeItem(at: url) }
        // Copies from a quarantined disk image must not carry the mark (FileManager.copyItem would).
        let marked = FileManager.default.temporaryDirectory.appendingPathComponent("launcher-\(getpid())-marked.zip")
        let copied = FileManager.default.temporaryDirectory.appendingPathComponent("launcher-\(getpid())-copied.zip")
        let viaFileManager = FileManager.default.temporaryDirectory.appendingPathComponent("launcher-\(getpid())-fm.zip")
        for url in [marked, copied, viaFileManager] { try? FileManager.default.removeItem(at: url) }
        try Data("payload".utf8).write(to: marked)
        let mark = "0081;00000000;Safari;"
        assert(setxattr(marked.path, "com.apple.quarantine", mark, mark.utf8.count, 0, 0) == 0)
        try Bootstrap.copyBytes(marked, to: copied)
        let copiedData = try Data(contentsOf: copied)
        assert(copiedData == Data("payload".utf8))
        assert(getxattr(copied.path, "com.apple.quarantine", nil, 0, 0, 0) < 0, "quarantine copied by copyBytes")
        try FileManager.default.copyItem(at: marked, to: viaFileManager)
        assert(getxattr(viaFileManager.path, "com.apple.quarantine", nil, 0, 0, 0) > 0, "expected FileManager to copy the mark")
        for url in [marked, copied, viaFileManager] { try? FileManager.default.removeItem(at: url) }
        // Earlier sessions are removed; anything else in the release folder is left alone.
        let base = FileManager.default.temporaryDirectory.appendingPathComponent("launcher-base-\(getpid())")
        let old = base.appendingPathComponent("session-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: old, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: base.appendingPathComponent("session-notauuid"), withIntermediateDirectories: true)
        try Data().write(to: base.appendingPathComponent("engine.zip"))
        Bootstrap.removeOldSessions(in: base)
        let left = try FileManager.default.contentsOfDirectory(atPath: base.path).sorted()
        assert(left == ["engine.zip", "session-notauuid"], "\(left)")
        try FileManager.default.removeItem(at: base)
        // Restore: only a folder holding catalog, kept apart from the installation folder.
        let disk = FileManager.default.temporaryDirectory.appendingPathComponent("launcher-backup-\(getpid())")
        let backup = disk.appendingPathComponent("backup one")
        try FileManager.default.createDirectory(at: backup.appendingPathComponent("catalog"), withIntermediateDirectories: true)
        let home = "/Users/someone/FamilyFoto"
        assert(Bootstrap.backupProblem(backup.path, installFolder: home) == nil)
        assert(Bootstrap.backupProblem("", installFolder: home)!.contains("Choose the backup"))
        assert(Bootstrap.backupProblem(disk.path, installFolder: home)!.contains("not a FamilyFoto backup"))
        assert(Bootstrap.backupProblem(backup.path, installFolder: backup.appendingPathComponent("FamilyFoto").path)!.contains("outside"))
        assert(Bootstrap.backupProblem(backup.path, installFolder: disk.path)!.contains("outside"))
        assert(Bootstrap.backupProblem(backup.path, installFolder: backup.path)!.contains("outside"))
        // Aliases: a link to the backup or to a folder inside it, and another capitalisation.
        try FileManager.default.createDirectory(at: backup.appendingPathComponent("sub"), withIntermediateDirectories: true)
        let toBackup = disk.appendingPathComponent("to-backup"), toSub = disk.appendingPathComponent("to-sub")
        try FileManager.default.createSymbolicLink(at: toBackup, withDestinationURL: backup)
        try FileManager.default.createSymbolicLink(at: toSub, withDestinationURL: backup.appendingPathComponent("sub"))
        assert(Bootstrap.backupProblem(backup.path, installFolder: toBackup.appendingPathComponent("new").path)!.contains("outside"))
        assert(Bootstrap.backupProblem(backup.path, installFolder: toSub.appendingPathComponent("new").path)!.contains("outside"))
        assert(Bootstrap.backupProblem(toBackup.path, installFolder: backup.appendingPathComponent("new").path)!.contains("outside"))
        let otherCase = disk.appendingPathComponent("BACKUP ONE").appendingPathComponent("new").path
        assert(Bootstrap.backupProblem(backup.path, installFolder: otherCase)!.contains("outside"))  // APFS is case-insensitive
        assert(Bootstrap.backupProblem(toBackup.path, installFolder: disk.appendingPathComponent("FamilyFoto").path) == nil)
        try FileManager.default.removeItem(at: disk)
        print("launcher tests passed")
    }
}
