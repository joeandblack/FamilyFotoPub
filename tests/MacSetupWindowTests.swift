// Compile alongside SetupWindow.swift with -parse-as-library -D SETUP_WINDOW_TEST.
import AppKit

final class PasteReceiver: NSResponder {
    var receivedPaste = false
    override var acceptsFirstResponder: Bool { true }
    @objc func paste(_ sender: Any?) { receivedPaste = true }
}

@main
struct WindowTests {
    static func main() {
        if CommandLine.arguments.count == 4 && CommandLine.arguments[1] == "--contract" {
            var a = Answers()
            a.restore = true; a.backup = CommandLine.arguments[2]; a.parent = CommandLine.arguments[3]
            a.backupPassword = "fixture administrator password"
            let data = try! JSONSerialization.data(withJSONObject: ["arguments": a.arguments(),
                "frame": try! passwordFrame(a.backupPassword).base64EncodedString()])
            FileHandle.standardOutput.write(data)
            return
        }
        // Exercise the real menu key-equivalent route without reading or changing the clipboard.
        let app = NSApplication.shared
        let menu = setupMenu()
        app.mainMenu = menu
        let receiver = PasteReceiver()
        let oldResponder = app.nextResponder
        app.nextResponder = receiver
        let pasteEvent = NSEvent.keyEvent(with: .keyDown, location: .zero,
            modifierFlags: .command, timestamp: 0, windowNumber: 0, context: nil,
            characters: "v", charactersIgnoringModifiers: "v", isARepeat: false, keyCode: 9)!
        assert(menu.performKeyEquivalent(with: pasteEvent))
        assert(receiver.receivedPaste)
        app.nextResponder = oldResponder

        let suggested = Setup(root: FileManager.default.temporaryDirectory, restore: true,
                              initialBackup: "/Volumes/Backup Disk/backup-one")
        assert(suggested.a.backup == "/Volumes/Backup Disk/backup-one")
        assert(suggested.a.copy && !suggested.passwordValid && !suggested.ready)
        assert(suggested.a.parent == "/Volumes/Backup Disk") // Copy defaults beside the backup too.
        suggested.a.copy = false
        assert(suggested.a.parent == "/Volumes/Backup Disk")
        suggested.selectPath("/Volumes/Another Disk/backup-two", for: \.backup)
        assert(suggested.a.backup == "/Volumes/Another Disk/backup-two")
        assert(suggested.a.parent == "/Volumes/Another Disk")
        let newLibrary = Setup(root: FileManager.default.temporaryDirectory, restore: false,
                               initialBackup: "/Volumes/Backup Disk/backup-one")
        assert(newLibrary.a.backup.isEmpty)
        let broken = Pipe()
        broken.fileHandleForReading.closeFile()
        do {
            try sendInput(Data("fixture".utf8), to: broken.fileHandleForWriting)
            assertionFailure("A closed password pipe must throw")
        } catch {}
        let badJournal = Setup(root: FileManager.default.temporaryDirectory, restore: true,
                               resumeCheck: { _ in ["resume": "The restore journal could not be read."] })
        badJournal.retry()
        assert(badJournal.page == .result && badJournal.message.contains("journal could not be read"))
        let defaults = Setup(root: FileManager.default.temporaryDirectory, restore: true)
        defaults.selectPath("/Volumes/Backup Disk/backups/backup-one", for: \.backup)
        assert(defaults.a.parent == "/Volumes/Backup Disk/backups") // Copy defaults beside the backup.
        defaults.checking = false
        defaults.a.copy = false
        assert(defaults.checking && !defaults.ready)
        assert(defaults.a.parent == "/Volumes/Backup Disk/backups")
        let restoreArgs = defaults.a.arguments()
        let mediaIndex = restoreArgs.firstIndex(of: "--media-folder")!
        assert(restoreArgs[mediaIndex + 1] == "/Volumes/Backup Disk/backups/FFLibrary")
        defaults.checking = false
        defaults.selectPath("/Volumes/Other Disk/backup-two", for: \.backup)
        assert(defaults.checking && !defaults.ready)
        assert(defaults.a.parent == "/Volumes/Other Disk")
        // Explicit selection of even the default path must be preserved thereafter.
        defaults.selectPath(defaults.a.parent, for: \.parent)
        defaults.selectPath("/Volumes/Third Disk/backup-three", for: \.backup)
        assert(defaults.a.parent == "/Volumes/Other Disk")
        defaults.selectPath("/Volumes/Third Disk/Custom", for: \.parent)
        defaults.a.copy = true
        defaults.a.copy = false
        assert(defaults.a.parent == "/Volumes/Third Disk/Custom")
        let firstMode = Setup(root: FileManager.default.temporaryDirectory, restore: true)
        assert(firstMode.a.parent.isEmpty) // No backup yet, so no default.
        firstMode.a.copy = false
        assert(firstMode.a.parent.isEmpty)
        firstMode.checking = false
        firstMode.selectPath("/Volumes/Backup Disk/backup", for: \.backup)
        assert(firstMode.checking && !firstMode.ready)
        assert(firstMode.a.parent == "/Volumes/Backup Disk")

        let setup = Setup(root: FileManager.default.temporaryDirectory, restore: false, resumeCheck: { _ in ["needsPassword": "true"] })
        setup.checking = false
        setup.a.password = "a long test password"
        assert(!setup.ready)
        setup.a.repeatPassword = setup.a.password
        assert(setup.ready)
        let args = setup.a.arguments()
        assert(args.contains("--password-stdin") && !args.contains(setup.a.password))
        setup.a.restore = true
        setup.checking = false
        assert(!setup.a.stopped && !setup.a.acceptDisk)
        assert(!setup.ready)
        setup.a.backupPassword = "fixture administrator password"
        setup.passwordValid = true
        assert(!setup.a.checkInput.keys.contains("password"))
        assert(!setup.a.arguments().contains(setup.a.backupPassword))
        assert(setup.a.arguments().contains("--backup-password-frame-stdin"))
        let frame = try! passwordFrame(setup.a.backupPassword)
        assert(frame.prefix(4).reduce(0) { ($0 << 8) | Int($1) } == frame.count - 4)
        let decoded = try! JSONSerialization.jsonObject(with: frame.dropFirst(4)) as! [String: String]
        assert(decoded["password"] == setup.a.backupPassword)
        assert(!setup.ready && setup.validationIssues.contains { $0.hasPrefix("Stop FamilyFoto on the old computer") })
        setup.a.stopped = true
        assert(setup.ready && !setup.a.arguments().contains("--allow-unprotected-media"))
        setup.a.stopped = false
        assert(!setup.ready)
        setup.a.stopped = true
        assert(setup.ready && !setup.a.arguments().contains("--password-stdin"))
        setup.a.people = true
        assert(!setup.ready)
        setup.a.terms = true
        assert(setup.ready && setup.a.arguments().contains("--research-only"))
        setup.errors = ["unprotected": "exFAT"]
        assert(!setup.ready && setup.validationIssues.contains("exFAT"))
        setup.a.acceptDisk = true
        assert(setup.ready && setup.a.arguments().contains("--allow-unprotected-media"))
        // Choosing another library location withdraws the acceptance.
        setup.selectPath("/Volumes/Other Disk", for: \.parent)
        assert(!setup.a.acceptDisk && !setup.a.arguments().contains("--allow-unprotected-media"))
        setup.checking = false
        setup.errors = ["unprotected": "exFAT"]
        assert(!setup.ready)
        setup.a.acceptDisk = true
        setup.errors = ["password": "Incorrect password."]
        assert(!setup.ready && setup.validationIssues.contains("Incorrect password."))
        setup.checking = true
        setup.errors = [:]
        assert(!setup.ready)
        setup.checking = false
        assert(setup.ready && setup.validationIssues.isEmpty)
        // Reselecting a path must check the disk again even when answers are unchanged.
        setup.selectPath("/tmp/backup", for: \.backup)
        assert(setup.checking && !setup.ready)
        setup.checking = false
        setup.errors = ["backup": "Old disk error"]
        let beforeReselection = setup.a
        setup.selectPath("/tmp/backup", for: \.backup)
        assert(setup.a == beforeReselection && setup.checking && !setup.ready)
        for field in [\Answers.backup, \Answers.parent] {
            setup.checking = false
            setup.selectPath(setup.a[keyPath: field], for: field)
            assert(setup.checking)
        }
        setup.consume("@@ {\"stage\":\"restored\",\"state\":\"running\"}")
        assert(setup.states["install"] == "done" && setup.states["restored"] == "running")
        setup.consume("@@ {\"phase\":\"copy\",\"files\":1,\"total_files\":2,\"bytes\":50,\"total_bytes\":100}")
        assert(setup.fraction == 0.5 && setup.count.contains("1 of 2 files"))
        setup.a.copy = false
        assert(setup.a.locationHint.contains("same disk"))
        assert(setup.a.locationHint.contains("outside the backup"))
        setup.consume("plain output")
        assert(setup.details.contains("plain output"))
        // People is offered only when a release downloaded it; terminal installs are unaffected.
        assert(Setup.downloadedPeople([:]))
        let receipt = FileManager.default.temporaryDirectory.appendingPathComponent("people-receipt-\(ProcessInfo.processInfo.processIdentifier).json")
        try! Data("{\"features\": [\"core\"]}".utf8).write(to: receipt)
        assert(!Setup.downloadedPeople(["FAMILYFOTO_PAYLOAD_RECEIPT": receipt.path]))
        try! Data("{\"features\": [\"core\", \"people\"]}".utf8).write(to: receipt)
        assert(Setup.downloadedPeople(["FAMILYFOTO_PAYLOAD_RECEIPT": receipt.path]))
        assert(!Setup.downloadedPeople(["FAMILYFOTO_PAYLOAD_RECEIPT": receipt.path + ".missing"]))
        try? FileManager.default.removeItem(at: receipt)
        // Opened by FamilyFoto Setup.app: its choice stands and People is on if it was downloaded.
        let fromApp = Setup(root: FileManager.default.temporaryDirectory, restore: true,
                            initialBackup: "/Volumes/Backup Disk/backup-one", peopleAvailable: true, modeChosen: true)
        assert(fromApp.modeChosen && fromApp.a.restore && fromApp.a.people && !fromApp.a.terms)
        assert(fromApp.a.backup == "/Volumes/Backup Disk/backup-one")
        let fromAppCore = Setup(root: FileManager.default.temporaryDirectory, restore: false, peopleAvailable: false, modeChosen: true)
        assert(!fromAppCore.a.restore && !fromAppCore.a.people)
        let fromTerminal = Setup(root: FileManager.default.temporaryDirectory, restore: false, peopleAvailable: true, modeChosen: false)
        assert(!fromTerminal.modeChosen && !fromTerminal.a.people)
        let corePack = Setup(root: FileManager.default.temporaryDirectory, restore: false, peopleAvailable: false)
        corePack.a.people = true
        assert(corePack.validationIssues.contains { $0.contains("People was not downloaded") })
        // Shared-installer download events show byte progress but never mark installation done.
        let downloads = Setup(root: FileManager.default.temporaryDirectory, restore: true)
        downloads.consume("@@ {\"schema_version\": 1, \"attempt_id\": \"a\", \"sequence\": 1, \"elapsed_ms\": 5, \"stage\": \"acquire\", \"state\": \"running\", \"component_id\": \"python\", \"completed\": 25000000, \"total\": 100000000, \"unit\": \"bytes\"}")
        assert(downloads.states["install"] == nil && downloads.states["acquire"] == nil)
        assert(downloads.fraction == 0.25 && downloads.count.contains("Downloading python") && downloads.count.contains("25.0 of 100.0 MB"))
        downloads.consume("@@ {\"schema_version\": 1, \"stage\": \"acquire\", \"state\": \"running\", \"component_id\": \"models\", \"completed\": 3, \"total\": 9, \"unit\": \"files\"}")
        assert(downloads.fraction == nil && downloads.count == "Downloading models")
        downloads.consume("@@ {\"schema_version\": 1, \"stage\": \"acquire\", \"state\": \"completed\", \"component_id\": \"python\"}")
        assert(downloads.count == "Downloaded python" && downloads.fraction == nil)
        downloads.consume("@@ {\"schema_version\": 1, \"stage\": \"prepare\", \"state\": \"completed\"}")
        assert(downloads.count == "Downloads verified" && downloads.states.isEmpty)
        downloads.consume("@@ {\"schema_version\": 2, \"stage\": \"acquire\", \"state\": \"running\", \"completed\": 1, \"total\": 2, \"unit\": \"bytes\"}")
        assert(downloads.count == "Working" && downloads.fraction == nil)
        setup.passwordValid = true
        setup.a.backupPassword += "changed"
        assert(!setup.passwordValid)
        setup.finish(2, "")
        assert(setup.a.backupPassword.isEmpty && !setup.passwordValid)
        setup.retry()
        assert(setup.page == .questions && !setup.ready)
        setup.finish(3, "Wrong password")
        assert(setup.code == 3 && setup.message.contains("enter its administrator password"))
        setup.finish(4, "")
        assert(setup.code == 4 && setup.message.contains("macos-people-install.json"))
        setup.finish(1, "Restore stopped: fixture")
        assert(setup.message.contains("Restore stopped: fixture"))
    }
}
