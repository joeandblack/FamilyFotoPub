import AppKit
import SwiftUI

private let python = CommandLine.arguments.count > 3 ? CommandLine.arguments[3] : "/opt/homebrew/opt/python@3.12/bin/python3.12"
private let stages = [
    ("install", "Installing FamilyFoto"), ("restored", "Restoring the catalog and media"),
    ("checked", "Checking the restored library"), ("switched", "Switching on the library"),
    ("thumbnails", "Restoring thumbnails and posters"), ("installed", "Moving the library into place"),
    ("people", "Setting up People"), ("started", "Starting FamilyFoto"),
    ("repaired", "Rebuilding missing thumbnails")
]

struct Answers: Equatable {
    // The switch confirmation and the unprotected-disk acceptance are the owner's own choices: never preset.
    var restore = false, copy = true, people = false, terms = false, stopped = false, acceptDisk = false
    var backup = "", backupPassword = "", parent = "", password = "", repeatPassword = ""
    var database = "5433", web = "4341", peoplePort = "54341", instance = ""

    var checkInput: [String: Any] {
        ["restore": restore, "copy": copy, "backup": backup, "parent": parent,
         "database": database, "web": web, "peoplePort": peoplePort, "instance": instance]
    }
    var locationHint: String {
        copy ? "By default, FFLibrary is created beside the backup. Use Browse to choose another folder with room for a second copy of the media."
             : "By default, FFLibrary is created beside the backup. Use Browse to choose another parent folder on the same disk, outside the backup folder. Setup moves the media with one folder rename."
    }
    var passwordIssue: String {
        if password.count < 12 { return "Use at least 12 characters." }
        if password.contains(where: { $0.isNewline }) { return "Use a single line for the password." }
        return password == repeatPassword ? "" : "The passwords must match."
    }
    func arguments() -> [String] {
        var args = [restore ? "restore" : "install", "--database-port", database, "--web-port", web,
                    "--people-port", peoplePort, "--progress-lines"]
        if !instance.isEmpty { args += ["--instance", instance] }
        args += people ? ["--with-people", "--research-only"] : ["--without-people"]
        if restore {
            args += ["--backup", backup, "--backup-password-frame-stdin", "--media", copy ? "copy" : "in-place",
                     "--media-folder", URL(fileURLWithPath: parent).appendingPathComponent("FFLibrary").path,
                     "--old-library-stopped"]
            if acceptDisk { args.append("--allow-unprotected-media") }
        } else { args.append("--password-stdin") }
        return args
    }
}

func passwordFrame(_ password: String) throws -> Data {
    let payload = try JSONSerialization.data(withJSONObject: ["password": password])
    var length = UInt32(payload.count).bigEndian
    var result = withUnsafeBytes(of: &length) { Data($0) }
    result.append(payload)
    return result
}

func sendInput(_ data: Data, to handle: FileHandle) throws {
    // Report EPIPE through the throwing API instead of terminating the window with SIGPIPE.
    signal(SIGPIPE, SIG_IGN)
    defer { try? handle.close() }
    try handle.write(contentsOf: data)
}

private func check(_ root: URL, _ action: String, _ input: [String: Any]) throws -> [String: String] {
    let child = Process(), output = Pipe(), pipe = Pipe()
    child.executableURL = URL(fileURLWithPath: python)
    child.arguments = ["-B", root.appendingPathComponent("installer/macos/setup_checks.py").path, action]
    child.standardInput = pipe
    child.standardOutput = output
    try child.run()
    do {
        try sendInput(action == "layout" ? Data() : JSONSerialization.data(withJSONObject: input), to: pipe.fileHandleForWriting)
    } catch {
        if child.isRunning { child.terminate() }
        child.waitUntilExit()
        throw error
    }
    let data = output.fileHandleForReading.readDataToEndOfFile()
    child.waitUntilExit()
    return try JSONDecoder().decode([String: String].self, from: data)
}

final class Setup: ObservableObject {
    enum Page { case questions, running, result }
    let root: URL
    let resumeCheck: (URL) throws -> [String: String]
    @Published var a = Answers() {
        didSet {
            if a.backup != oldValue.backup || a.backupPassword != oldValue.backupPassword || a.restore != oldValue.restore {
                passwordRevision += 1
                passwordValid = false
                passwordError = ""
            }
            if a.parent != oldValue.parent || a.backup != oldValue.backup || a.copy != oldValue.copy || a.restore != oldValue.restore {
                // An acceptance covers only the disk it was given for.
                a.acceptDisk = false
            }
            if page == .questions && !resumed && !mediaParentSelected && a.restore {
                let defaultParent = Setup.defaultParent(a.backup)
                if a.parent != defaultParent {
                    a.parent = defaultParent
                }
            }
            if page == .questions && !resumed && !NSDictionary(dictionary: oldValue.checkInput).isEqual(to: a.checkInput) {
                validate()
            }
        }
    }
    @Published var page = Page.questions
    @Published var errors = [String: String]()
    @Published var showValidationErrors = false
    @Published var checking = true
    @Published var passwordValid = false
    @Published var passwordChecking = false
    @Published var passwordNeeded = true
    @Published var passwordError = ""
    private var passwordRevision = 0
    private var pendingSwitch = false
    @Published var terms = ""
    @Published var details = ""
    @Published var count = ""
    @Published var fraction: Double?
    @Published var states = [String: String]()
    @Published var code: Int32 = 1
    @Published var message = ""
    @Published var address = ""
    @Published var certificate = ""
    @Published var advanced = false
    @Published var showDetails = false
    @Published var confirmSwitch = false
    @Published var trustMessage = ""
    @Published var resumed = false
    var arguments = [String]()
    private var mediaParentSelected = false
    private var revision = 0, lastLine = "", reason = ""

    /// A release installation can install People only if its People payloads were downloaded.
    let peopleAvailable: Bool
    /// FamilyFoto Setup.app already asked New library or Restore (and People); don't ask again.
    let modeChosen: Bool

    init(root: URL, restore: Bool, initialBackup: String = "", peopleAvailable: Bool = Setup.downloadedPeople(),
         modeChosen: Bool = ProcessInfo.processInfo.environment["FAMILYFOTO_LAUNCHER"] == "1",
         resumeCheck: @escaping (URL) throws -> [String: String] = { try check($0, "resume", [:]) }) {
        self.root = root
        self.resumeCheck = resumeCheck
        self.peopleAvailable = peopleAvailable
        self.modeChosen = modeChosen
        a.restore = restore
        // People was downloaded because it was chosen in the Setup app; its model terms are still asked here.
        if modeChosen && peopleAvailable { a.people = true }
        // Property observers do not run in init, so apply the default library location here.
        if restore { a.backup = initialBackup; a.parent = Setup.defaultParent(initialBackup) }
    }
    /// False only for a shared-installer release whose receipt lacks the People feature.
    static func downloadedPeople(_ environment: [String: String] = ProcessInfo.processInfo.environment) -> Bool {
        guard let path = environment["FAMILYFOTO_PAYLOAD_RECEIPT"] else { return true }
        guard let data = FileManager.default.contents(atPath: path),
              let receipt = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let features = receipt["features"] as? [String] else { return false }
        return features.contains("people")
    }
    /// FFLibrary goes beside the backup folder unless the user chooses another parent.
    static func defaultParent(_ backup: String) -> String {
        backup.isEmpty ? "" : URL(fileURLWithPath: backup).deletingLastPathComponent().path
    }
    func load() {
        let record = root.appendingPathComponent(".macos-setup.json")
        if FileManager.default.fileExists(atPath: record.path) {
            do {
                arguments = try JSONDecoder().decode([String].self, from: Data(contentsOf: record))
                resumed = true
                a.restore = arguments.first == "restore"
                if let i = arguments.firstIndex(of: "--web-port"), i + 1 < arguments.count { a.web = arguments[i+1] }
                if a.restore {
                    if let i = arguments.firstIndex(of: "--backup"), i + 1 < arguments.count { a.backup = arguments[i+1] }
                    retry()
                } else if FileManager.default.fileExists(atPath: root.appendingPathComponent("runtime/macos-database-ready.json").path) {
                    start()
                } else { checking = false }
            } catch { page = .result; message = "The setup record could not be read." }
            return
        }
        do {
            let layout = try check(root, "layout", [:])
            a.database = layout["database"] ?? ""; a.web = layout["web"] ?? ""
            a.peoplePort = layout["peoplePort"] ?? ""; a.instance = layout["instance"] ?? ""
            terms = layout["terms"] ?? ""
        } catch { errors = ["layout": "Could not read installation settings."]; return }
        validate()
    }
    func selectPath(_ path: String, for field: WritableKeyPath<Answers, String>) {
        if field == \Answers.parent { mediaParentSelected = true }
        if field == \Answers.backup {
            passwordRevision += 1; passwordValid = false; passwordError = ""
        }
        if a[keyPath: field] == path {
            // File contents or permissions may have changed since the last check.
            validate()
        } else {
            a[keyPath: field] = path // didSet schedules validation for changed paths.
        }
    }
    func validate() {
        guard page == .questions, !resumed else { return }
        revision += 1
        let current = revision, input = a.checkInput
        checking = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.15) {
            guard current == self.revision else { return }
            DispatchQueue.global().async {
                let result = (try? check(self.root, "check", input)) ?? ["layout": "Could not check the installation."]
                DispatchQueue.main.async {
                    guard current == self.revision else { return }
                    self.errors = result
                    self.checking = false
                }
            }
        }
    }
    func checkPassword() {
        guard !passwordChecking else { return }
        guard a.backupPassword.utf8.count <= 1024 else {
            passwordValid = false; passwordError = "The password can be at most 1024 UTF-8 bytes long."; return
        }
        passwordChecking = true; passwordValid = false; passwordError = ""
        let current = passwordRevision
        let input = ["backup": a.backup, "password": a.backupPassword]
        DispatchQueue.global().async {
            let result = (try? check(self.root, "password", input)) ?? ["password": "Could not check the password. Reconnect the backup and try again."]
            DispatchQueue.main.async {
                self.passwordChecking = false
                guard current == self.passwordRevision else { return }
                self.passwordValid = result.isEmpty
                self.passwordError = result.values.sorted().joined(separator: "\n")
            }
        }
    }
    func retry(switchConfirmed: Bool = false) {
        pendingSwitch = pendingSwitch || switchConfirmed
        if !a.restore { start(); return }
        do {
            let result = try resumeCheck(root)
            guard let needed = result["needsPassword"], ["true", "false"].contains(needed) else {
                finish(1, result["resume"] ?? "Could not check restore progress. Keep this installation folder and inspect restore/MOVE-IN.json.")
                return
            }
            passwordNeeded = needed == "true"
        } catch {
            finish(1, "Could not check restore progress. Keep this installation folder and inspect restore/MOVE-IN.json.")
            return
        }
        if !passwordNeeded { start(); return }
        resumed = true; checking = false; errors = [:]
        passwordValid = false; a.backupPassword = ""; page = .questions
    }
    var validationIssues: [String] {
        var issues = errors.keys.sorted().compactMap { key -> String? in
            key == "unprotected" && a.acceptDisk ? nil : errors[key]
        }
        if !resumed && a.people && !a.terms { issues.append("Accept the People model terms, or turn off Install People.") }
        if !resumed && a.people && !peopleAvailable { issues.append("People was not downloaded. Turn off Install People.") }
        if a.restore && !a.stopped { issues.append("Stop FamilyFoto on the old computer, then confirm it is stopped.") }
        if a.restore && passwordNeeded && !passwordValid {
            issues.append(passwordError.isEmpty ? "Enter and check the administrator password from when the backup was made." : passwordError)
        }
        if !a.restore && !a.passwordIssue.isEmpty { issues.append(a.passwordIssue) }
        return issues
    }
    var ready: Bool { !checking && !passwordChecking && validationIssues.isEmpty }
    func start(switchConfirmed: Bool = false) {
        guard page != .running else { return }
        if arguments.isEmpty { arguments = a.arguments() }
        reason = ""; lastLine = ""; details = ""; count = ""; fraction = nil
        states = ["install": "running"]; page = .running
        if let journal = try? JSONSerialization.jsonObject(with: Data(contentsOf: root.appendingPathComponent("restore/MOVE-IN.json"))) as? [String: Any],
           let done = journal["stages"] as? [String: Any] {
            states["install"] = "done"
            for key in done.keys { states[key] = "done" }
        }
        let payload: Data
        do {
            payload = a.restore ? (passwordNeeded ? try passwordFrame(a.backupPassword) : Data()) : Data((a.password + "\n").utf8)
        } catch {
            finish(1, "Setup could not prepare the password message. Close and reopen setup to try again.")
            return
        }
        let child = Process(), output = Pipe(), input = Pipe()
        child.executableURL = URL(fileURLWithPath: python)
        let options = (switchConfirmed || pendingSwitch) ? ["--confirm-switch"] : arguments
        child.arguments = ["-B", root.appendingPathComponent("installer/macos/setup_run.py").path] + options
        child.standardOutput = output; child.standardError = output; child.standardInput = input
        do { try child.run() }
        catch { finish(1, error.localizedDescription); return }
        pendingSwitch = false
        var inputError = ""
        do { try sendInput(payload, to: input.fileHandleForWriting) }
        catch { inputError = "Setup could not send the password to its helper. Close and reopen setup to try again." }
        let handoffError = inputError
        DispatchQueue.global().async {
            var pending = Data()
            while true {
                let chunk = output.fileHandleForReading.availableData
                pending.append(chunk)
                var lines = [String]()
                while let end = pending.firstIndex(of: 10) {
                    lines.append(String(decoding: pending[..<end], as: UTF8.self))
                    pending.removeSubrange(...end)
                }
                if chunk.isEmpty && !pending.isEmpty { lines.append(String(decoding: pending, as: UTF8.self)) }
                let batch = lines
                DispatchQueue.main.async { for line in batch { self.consume(line) } }
                if chunk.isEmpty { break }
            }
            child.waitUntilExit()
            DispatchQueue.main.async { self.finish(handoffError.isEmpty ? child.terminationStatus : 1, handoffError.isEmpty ? (self.reason.isEmpty ? self.lastLine : self.reason) : handoffError) }
        }
    }
    func consumeShared(_ event: [String: Any], version: Int) {
        let stage = event["stage"] as? String, state = event["state"] as? String
        let component = event["component_id"] as? String ?? "components"
        fraction = nil
        guard version == 1 else { count = "Working"; return }
        switch (stage, state) {
        case ("acquire", "completed"):
            count = "Downloaded \(component)"
        case ("acquire", _):
            count = "Downloading \(component)"
            let done = (event["completed"] as? NSNumber)?.doubleValue
            let total = (event["total"] as? NSNumber)?.doubleValue
            // Only bytes are defined; never guess a percentage for another or a missing unit.
            if event["unit"] as? String == "bytes", let n = done, let t = total, t > 0 {
                fraction = min(1, n / t)
                count += String(format: " · %.1f of %.1f MB", n / 1e6, t / 1e6)
            }
        case ("prepare", "completed"):
            count = "Downloads verified"
        default:
            count = "Working"
        }
    }

    func consume(_ line: String) {
        details += line + "\n"
        if details.count > 32000 { details = String(details.suffix(24000)) }
        if !line.isEmpty { lastLine = line }
        if line.hasPrefix("Restore stopped") || line.hasPrefix("Installation stopped") { reason = line }
        guard line.hasPrefix("@@ "), let event = try? JSONSerialization.jsonObject(with: Data(line.dropFirst(3).utf8)) as? [String: Any] else { return }
        // Shared-installer events (installer/common/README.md) carry schema_version; dispatch on it
        // before the legacy stage/phase handling, so a download never marks installation done.
        if let version = event["schema_version"] as? Int {
            consumeShared(event, version: version)
            return
        }
        if let stage = event["stage"] as? String, let state = event["state"] as? String {
            states["install"] = "done"; states[stage] = state; count = ""; fraction = nil
        } else {
            let files = (event["files"] ?? event["done_files"]) as? Int
            let bytes = (event["bytes"] ?? event["done_bytes"]) as? Double
            var parts = [event["phase"] as? String ?? "Working"]
            if let n = files {
                parts.append("\(n.formatted())" + ((event["total_files"] as? Int).map { " of \($0.formatted())" } ?? "") + " files")
            }
            if let n = bytes, let total = event["total_bytes"] as? Double, total > 0 {
                fraction = min(1, n / total)
                parts.append(String(format: "%.1f of %.1f GB", n/1e9, total/1e9))
            }
            count = parts.joined(separator: " · ")
        }
    }
    func finish(_ status: Int32, _ why: String) {
        code = status; page = .result
        a.backupPassword = ""; passwordValid = false
        if let data = try? Data(contentsOf: root.appendingPathComponent(".macos-setup.json")),
           let recorded = try? JSONDecoder().decode([String].self, from: data) { arguments = recorded }
        if status == 0 {
            a.password = ""; a.repeatPassword = ""
            address = "https://127.0.0.1:\(a.web)/"
            certificate = root.appendingPathComponent("config/tls/ca.pem").path
            message = "FamilyFoto is running. Sign in as family with the password you chose."
            if a.restore {
                let report = (try? JSONSerialization.jsonObject(with: Data(contentsOf: root.appendingPathComponent("restore/MOVE-IN-REPORT.json")))) as? [String: Any]
                address = report?["url"] as? String ?? address
                certificate = report?["certificate_authority"] as? String ?? certificate
                message = "FamilyFoto is running with the restored library."
                if let n = report?["media_files"] as? Int { message += " Media files: \(n.formatted())." }
                message += "\n\nSign in with the old library’s accounts and passwords."
                if let people = report?["people"] as? [String: Any] {
                    // As app.migration.move_in.people_summary: a check record, "no-stored-faces" or "skipped".
                    let check = people["library_check"]
                    if (check as? [String: Any])?["passed"] as? Bool == true {
                        message += "\nPeople is ready to edit."
                    } else if check as? String == "no-stored-faces" {
                        message += "\nPeople is ready to edit. The library had no saved faces yet, so there was nothing to check."
                    } else if check is [String: Any] {
                        message += "\nThe People check did not pass. Saved people are viewable."
                    } else {
                        message += "\nPeople is installed. Its check runs at the first Discover people; until it passes, saved people are viewable."
                    }
                } else { message += "\nSaved people are viewable. Install People to edit them." }
            }
        } else if status == 3 {
            message = why + "\n\nReconnect the recorded backup and enter its administrator password to continue."
        } else if status == 4 {
            message = "People installation was interrupted. Close setup and keep this installation folder. Review logs/macos-setup.log and runtime/macos-people-install.json with the person maintaining FamilyFoto before repairing People."
        } else if status == 2 {
            message = "The library was restored and checked. Stop FamilyFoto on the old computer before switching on this library."
        } else { message = (why.isEmpty ? "Setup exited with code \(status)." : why) + "\n\nSetup kept the existing files." }
    }
}

struct SetupView: View {
    @ObservedObject var setup: Setup

    func picker(_ label: String, _ field: WritableKeyPath<Answers, String>, folder: Bool) -> some View {
        HStack {
            Text(label).frame(width: 115, alignment: .leading)
            Text(setup.a[keyPath: field].isEmpty ? "Choose…" : setup.a[keyPath: field])
                .foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle).help(setup.a[keyPath: field])
            Spacer()
            Button("Browse…") {
                let panel = NSOpenPanel()
                panel.canChooseDirectories = folder; panel.canChooseFiles = !folder
                panel.allowsMultipleSelection = false; panel.canCreateDirectories = false
                if panel.runModal() == .OK, let url = panel.url {
                    setup.selectPath(folder ? url.resolvingSymlinksInPath().path : url.path, for: field)
                }
            }.accessibilityLabel("Choose " + label)
        }
    }
    func hint(_ key: String) -> some View {
        Text(setup.errors[key] ?? "").font(.callout).foregroundStyle(.red).textSelection(.enabled)
            .frame(height: setup.errors[key] == nil ? 0 : nil)
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(setup.page == .questions ? "Set up FamilyFoto" : setup.page == .running ? "Setting up FamilyFoto" :
                    setup.code == 0 ? "FamilyFoto is ready" : setup.code == 2 ? "Ready to switch on" : "Setup stopped")
                .font(.title.bold())
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 10) {
                    if setup.page == .questions {
                        if !setup.resumed && !setup.modeChosen {
                            Picker("Library", selection: $setup.a.restore) {
                                Text("New library").tag(false); Text("Restore from a backup").tag(true)
                            }.pickerStyle(.segmented)
                        }
                        if setup.a.restore {
                            picker("Backup folder", \.backup, folder: true).disabled(setup.resumed); hint("backup")
                            SecureField("Administrator password from the old computer", text: $setup.a.backupPassword)
                                .onSubmit { setup.checkPassword() }
                            Text("Use the password from when this backup was made. It is never saved by setup.").font(.callout).foregroundStyle(.secondary)
                            HStack {
                                Button(setup.passwordChecking ? "Checking password…" : "Check password") { setup.checkPassword() }
                                    .disabled(setup.passwordChecking || setup.a.backup.isEmpty || setup.a.backupPassword.isEmpty)
                                if setup.passwordValid { Text("Password checked").foregroundStyle(.green) }
                            }
                            Text(setup.passwordError).font(.callout).foregroundStyle(.red)
                            if !setup.resumed {
                                Picker("Photos and videos", selection: $setup.a.copy) {
                                    Text("Copy").tag(true); Text("In place").tag(false)
                                }.pickerStyle(.radioGroup).horizontalRadioGroupLayout()
                                Text(setup.a.copy ? "Copy the photos and videos and keep the backup." :
                                     "The backup disk becomes the library disk. This uses up the backup.").font(.callout)
                                picker(setup.a.copy ? "Library location" : "On backup disk", \.parent, folder: true)
                                Text(setup.a.locationHint).font(.callout)
                                hint("parent")
                                if setup.errors["unprotected"] != nil {
                                    Toggle("I accept that anyone with this disk can read the photos and videos.", isOn: $setup.a.acceptDisk)
                                }
                                Toggle("The old computer’s FamilyFoto is stopped for good.", isOn: $setup.a.stopped)
                            }
                        } else {
                            SecureField("Administrator password", text: $setup.a.password)
                            SecureField("Repeat password", text: $setup.a.repeatPassword)
                            Text(setup.a.passwordIssue).font(.callout).foregroundStyle(.red)
                        }
                        if !setup.resumed {
                            Divider()
                            Toggle("Install People", isOn: $setup.a.people).disabled(!setup.peopleAvailable)
                            if !setup.peopleAvailable {
                                Text("People was not downloaded. To add it, open FamilyFoto Setup again and choose People.").font(.callout)
                            }
                            if setup.a.people {
                                Text(setup.terms).font(.callout)
                                Toggle("I accept these model terms.", isOn: $setup.a.terms)
                            } else if setup.a.restore { Text("Saved people will still be viewable.").font(.callout) }
                            DisclosureGroup("Advanced", isExpanded: $setup.advanced) {
                                VStack(alignment: .leading) {
                                    TextField("Installation name (optional)", text: $setup.a.instance)
                                    HStack {
                                        port("Database", $setup.a.database); port("Web", $setup.a.web); port("People", $setup.a.peoplePort)
                                    }
                                }.padding(.top, 6)
                            }
                            hint("layout")
                        }
                    } else if setup.page == .running {
                        ForEach(stages.filter { setup.a.restore || ["install","started"].contains($0.0) }, id: \.0) { stage in
                            Label(stage.1, systemImage: setup.states[stage.0] == "done" ? "checkmark.circle.fill" :
                                    setup.states[stage.0] == "running" ? "arrow.right.circle.fill" : "circle")
                        }
                        if let fraction = setup.fraction { ProgressView(value: fraction) } else { ProgressView().controlSize(.small) }
                        Text(setup.count)
                        Text("Keep this window open until setup finishes.").font(.callout)
                    } else {
                        Text(setup.message).textSelection(.enabled)
                        if setup.code == 0 {
                            Text(setup.address).textSelection(.enabled)
                            Button("Trust this computer’s certificate…") { trust() }
                            Text(setup.trustMessage.isEmpty ? "Use this if the browser warns about the certificate." : setup.trustMessage).font(.callout)
                        }
                    }
                    if setup.page != .questions {
                        DisclosureGroup("Show details", isExpanded: $setup.showDetails) {
                            Text(setup.details).font(.system(.caption, design: .monospaced)).textSelection(.enabled)
                        }
                    }
                }.frame(maxWidth: .infinity, alignment: .leading).padding(2)
            }
            Divider()
            if setup.page == .questions {
                if setup.checking {
                    HStack { ProgressView().controlSize(.small); Text("Checking your selections…") }
                } else if !setup.validationIssues.isEmpty {
                    HStack {
                        Label("Fix the validation errors to enable Start.", systemImage: "exclamationmark.triangle.fill")
                            .foregroundStyle(.red)
                        Spacer()
                        Button("Show errors…") { setup.showValidationErrors = true }
                    }
                }
            }
            HStack {
                Spacer()
                if setup.page != .running { Button("Close") { NSApp.terminate(nil) } }
                if setup.page == .questions {
                    Button("Start") { setup.start() }.disabled(!setup.ready).keyboardShortcut(.defaultAction)
                } else if setup.page == .result {
                    if setup.code == 0 { Button("Open FamilyFoto") {
                        if let url = URL(string: setup.address) { NSWorkspace.shared.open(url) }
                    }.keyboardShortcut(.defaultAction) }
                    else if setup.code == 2 { Button("Switch on…") { setup.confirmSwitch = true } }
                    else if setup.code != 4 && !setup.arguments.isEmpty { Button("Try again") { setup.retry() } }
                }
            }
        }.padding(22).frame(minWidth: 560, minHeight: 520)
        .alert("Setup needs your attention", isPresented: $setup.showValidationErrors) {
            Button("OK", role: .cancel) {}
            Button("Check again") { setup.validate() }
        } message: {
            Text(setup.validationIssues.joined(separator: "\n\n"))
        }
        .alert("Is FamilyFoto stopped on the old computer?", isPresented: $setup.confirmSwitch) {
            Button("Cancel", role: .cancel) {}
            Button("Switch on") { setup.retry(switchConfirmed: true) }
        }
    }
    func port(_ label: String, _ field: Binding<String>) -> some View {
        VStack(alignment: .leading) { Text(label).font(.caption); TextField(label, text: field) }
    }
    func trust() {
        let child = Process()
        child.executableURL = URL(fileURLWithPath: "/usr/bin/security")
        child.arguments = ["add-trusted-cert", "-r", "trustRoot", "-k",
                           NSHomeDirectory() + "/Library/Keychains/login.keychain-db", setup.certificate]
        child.terminationHandler = { process in DispatchQueue.main.async {
            setup.trustMessage = process.terminationStatus == 0 ? "Certificate trusted. Reload the browser." : "The certificate was not trusted."
        } }
        do { try child.run() } catch { setup.trustMessage = error.localizedDescription }
    }
}

// A manually hosted SwiftUI window needs an AppKit menu for standard key equivalents.
// Nil targets route editing to the focused field, preserving native secure-field behavior.
func setupMenu() -> NSMenu {
    let menu = NSMenu()
    let application = NSMenuItem()
    application.submenu = NSMenu(title: "FamilyFoto Setup")
    application.submenu?.addItem(withTitle: "Quit FamilyFoto Setup", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
    menu.addItem(application)
    let edit = NSMenuItem()
    edit.submenu = NSMenu(title: "Edit")
    for (title, action, key) in [
        ("Undo", Selector(("undo:")), "z"),
        ("Redo", Selector(("redo:")), "Z"),
        ("Cut", #selector(NSText.cut(_:)), "x"),
        ("Copy", #selector(NSText.copy(_:)), "c"),
        ("Paste", #selector(NSText.paste(_:)), "v"),
        ("Select All", #selector(NSText.selectAll(_:)), "a")
    ] {
        edit.submenu?.addItem(withTitle: title, action: action, keyEquivalent: key)
    }
    menu.addItem(edit)
    return menu
}

final class Delegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    let setup: Setup
    var window: NSWindow!
    init(_ setup: Setup) { self.setup = setup }
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.mainMenu = setupMenu()
        window = NSWindow(contentViewController: NSHostingController(rootView: SetupView(setup: setup)))
        window.title = "FamilyFoto Setup"
        window.setContentSize(NSSize(width: 650, height: 620)); window.center()
        window.delegate = self; window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        setup.load()
    }
    func windowShouldClose(_ sender: NSWindow) -> Bool { canClose() }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        canClose() ? .terminateNow : .terminateCancel
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
    func canClose() -> Bool {
        guard setup.page == .running else { return true }
        let alert = NSAlert(); alert.messageText = "FamilyFoto setup is running."
        alert.informativeText = "Wait until setup finishes or stops before closing this window."
        alert.runModal(); return false
    }
}

#if !SETUP_WINDOW_TEST
@main
struct SetupApplication {
    static func main() {
        let app = NSApplication.shared
        let model = Setup(root: URL(fileURLWithPath: CommandLine.arguments[1]), restore: CommandLine.arguments[2] == "restore",
                          initialBackup: ProcessInfo.processInfo.environment["FAMILYFOTO_RESTORE_BACKUP"] ?? "")
        let delegate = Delegate(model)
        app.setActivationPolicy(.regular)
        app.delegate = delegate
        app.run()
    }
}
#endif
