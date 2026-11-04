import XCTest
@testable import TokNotch

final class FleetTests: XCTestCase {
    private let sample = """
    {"agentVersion":1,"host":{"hostname":"box","system":"Linux","machine":"x86_64","cpus":16,"load":1.5,"cpuPercent":42.5,"temperature":61.2},
    "sessions":[{"id":"a","title":"t","kind":"interactive","model":"m","cwd":"~/x","lastTool":"Bash","idleSeconds":5,
    "usage":{"input":1,"output":2,"cacheRead":3,"cacheWrite":4}},
    {"id":"b","title":"","kind":"worker","model":"m","cwd":"","lastTool":"","idleSeconds":500,
    "usage":{"input":0,"output":0,"cacheRead":0,"cacheWrite":0}}],
    "todayByModel":{"m":{"input":1,"output":2,"cacheRead":3,"cacheWrite":4},"n":{"input":10,"output":0,"cacheRead":0,"cacheWrite":0}},
    "workers":[{"id":"1","provider":"minimax","state":"running","title":"x","elapsedSeconds":3,"steps":6,"lastTool":"Edit","lastFile":"foo.swift","model":"m","sessionId":"b","idleSeconds":400},
    {"id":"2","provider":"claude","state":"running","title":"y","elapsedSeconds":3,"remoteHost":"other-box"},
    {"id":"3","provider":"zhipu","state":"done","title":"z","elapsedSeconds":3,"usage":{"input":1,"output":1,"cacheRead":0,"cacheWrite":0}}]}
    """.replacingOccurrences(of: "\n", with: "")

    func testAWorkersSessionIsNotListedOrCountedTwice() throws {
        let snapshot = try XCTUnwrap(FleetSnapshot.decode(line: Data(sample.utf8)))
        XCTAssertEqual(snapshot.sessions.map(\.id), ["a", "b"])
        XCTAssertEqual(snapshot.ownSessions.map(\.id), ["a"])
    }

    func testWorkerProgress() throws {
        let snapshot = try XCTUnwrap(FleetSnapshot.decode(line: Data(sample.utf8)))
        let worker = try XCTUnwrap(snapshot.workers.first { $0.id == "1" })
        XCTAssertEqual(worker.steps, 6)
        XCTAssertEqual(worker.lastFile, "foo.swift")
        XCTAssertTrue(worker.isQuiet)
        XCTAssertTrue(worker.isCancellableHere)
    }

    /// A model goes by a short name or by its provider's full name, on that provider only.
    func testFullModelNamesAreAcceptedForTheirOwnProvider() {
        XCTAssertTrue(FleetRules.isAllowedModel("glm-5.1", provider: "zhipu"))
        XCTAssertTrue(FleetRules.isAllowedModel("MiniMax-M2.5", provider: "minimax"))
        XCTAssertTrue(FleetRules.isAllowedModel("claude-fable-5-1", provider: "claude"))
        XCTAssertFalse(FleetRules.isAllowedModel("glm-5.1", provider: "minimax"))
        XCTAssertFalse(FleetRules.isAllowedModel("sk-FAKE-KEY", provider: "zhipu"))
        XCTAssertFalse(FleetRules.isAllowedModel("glm-5; rm -rf", provider: "zhipu"))
    }

    func testMenusListWhatTheProviderOffersOnceEach() {
        let ids = { (listed: [String], current: String?, full: Bool) in
            FleetRules.menuModels(provider: "zhipu", listed: listed, current: current, acceptsFullNames: full)
                .map(\.id)
        }
        XCTAssertEqual(ids(["glm-5.3-flash", "glm-5.1", "other"], nil, true), ["flash", "glm-5.1"])
        XCTAssertEqual(ids(["glm-5.1"], nil, false), ["flash"], "an older mm-worker can't take full names")
        XCTAssertEqual(ids([], "glm-4.5", true), ["flash", "glm-4.5"], "the current choice stays on the menu")
    }

    func testHiddenModelsStayOffTheMenuUnlessAlreadyChosen() {
        let ids = { (current: String?) in
            FleetRules.menuModels(provider: "zhipu", listed: ["glm-5.3-flashx", "glm-5.1"], current: current,
                                  acceptsFullNames: true).map(\.id)
        }
        XCTAssertEqual(ids(nil), ["flash", "glm-5.1"])
        XCTAssertEqual(ids("glm-5.3-flashx"), ["flash", "glm-5.1", "glm-5.3-flashx"])
    }

    func testAnUnknownCLIIsAnIssueAndAbsentMeansClaude() throws {
        XCTAssertEqual(issues { $0.cli = nil }, [])
        XCTAssertEqual(issues { $0.cli = "opencode" }, [])
        XCTAssertEqual(issues { $0.cli = "nope" }, [.cli])
    }

    func testModelListingKeepsListsAndErrorsApart() {
        let output = "profile noise\n"
            + #"{"ok":true,"providers":{"zhipu":{"models":["glm-5"]},"qwen":{"error":"HTTP 404"}}}"#
        let listing = FleetModelListing(output: Data(output.utf8))
        XCTAssertEqual(listing?.models, ["zhipu": ["glm-5"]])
        XCTAssertEqual(listing?.errors, ["qwen": "HTTP 404"])
        XCTAssertNil(FleetModelListing(output: Data("nothing useful".utf8)))
    }

    /// ssh quits without reading when a host is unreachable, and the agent is larger
    /// than a pipe holds. Writing to it must fail, not end the app with a SIGPIPE.
    func testInputPipesReportAWriteErrorInsteadOfSIGPIPE() {
        let pipe = FleetConnection.inputPipe()
        XCTAssertEqual(fcntl(pipe.fileHandleForWriting.fileDescriptor, F_GETNOSIGPIPE), 1)
        try? pipe.fileHandleForReading.close()
        XCTAssertThrowsError(try pipe.fileHandleForWriting.write(contentsOf: Data([0])))
    }

    /// One sent elsewhere is stopped on the machine running it; finished ones are done.
    func testOnlyRunningLocalWorkersCanBeCancelledHere() throws {
        let snapshot = try XCTUnwrap(FleetSnapshot.decode(line: Data(sample.utf8)))
        XCTAssertEqual(snapshot.workers.filter(\.isCancellableHere).map(\.id), ["1"])
    }

    func testOnlyRealRunIDsAreAccepted() {
        XCTAssertTrue(FleetConnection.isRunID("20261101-012605-1064"))
        for bad in ["", "1-2", "1-2-3; rm -rf ~", "../x", "1-2-3\n", "$(id)-1-2"] {
            XCTAssertFalse(FleetConnection.isRunID(bad), bad)
        }
    }

    func testCancelCommandAndAnswers() {
        let device = FleetDevice(id: "p", label: "p", ssh: ["box"])
        XCTAssertEqual(FleetConnection.command(for: device, alias: "box", action: ["cancel", "1-2-3"]).arguments.last,
                       "python3 -I -u - 'cancel' '1-2-3'")
        XCTAssertNil(FleetConnection.cancelError(fromOutput: Data("noise\n{\"ok\": true}\n".utf8)))
        XCTAssertEqual(FleetConnection.cancelError(fromOutput: Data("{\"ok\": false, \"error\": \"not running\"}".utf8)),
                       "not running")
        XCTAssertNotNil(FleetConnection.cancelError(fromOutput: Data()))
    }

    func testDecodesAgentLine() throws {
        let snapshot = try XCTUnwrap(FleetSnapshot.decode(line: Data(sample.utf8)))
        XCTAssertEqual(snapshot.todayTotal, 20)
        XCTAssertEqual(snapshot.activeSessions.map(\.id), ["a"])
    }

    func testReadsCPUAndTemperature() throws {
        let snapshot = try XCTUnwrap(FleetSnapshot.decode(line: Data(sample.utf8)))
        XCTAssertEqual(snapshot.host.cpuPercent, 42.5)
        XCTAssertEqual(snapshot.host.temperature, 61.2)
    }

    /// A machine with no readable sensor still reports, and shows a dash rather than 0.
    func testMissingVitalsAreUnknownNotZero() throws {
        let without = sample.replacingOccurrences(of: ",\"cpuPercent\":42.5,\"temperature\":61.2", with: "")
        let snapshot = try XCTUnwrap(FleetSnapshot.decode(line: Data(without.utf8)))
        XCTAssertNil(snapshot.host.cpuPercent)
        XCTAssertNil(snapshot.host.temperature)
        XCTAssertEqual(FleetText.percent(snapshot.host.cpuPercent), "—")
        XCTAssertEqual(FleetText.degrees(snapshot.host.temperature), "—")
    }

    func testVitalLevels() {
        XCTAssertEqual(FleetVitalLevel.cpu(69.9), .normal)
        XCTAssertEqual(FleetVitalLevel.cpu(70), .warm)
        XCTAssertEqual(FleetVitalLevel.cpu(90), .hot)
        XCTAssertEqual(FleetVitalLevel.temperature(74.9), .normal)
        XCTAssertEqual(FleetVitalLevel.temperature(75), .warm)
        XCTAssertEqual(FleetVitalLevel.temperature(90), .hot)
    }

    func testVitalsRoundForDisplay() {
        XCTAssertEqual(FleetText.percent(0), "<1%")
        XCTAssertEqual(FleetText.percent(0.9), "<1%")
        XCTAssertEqual(FleetText.percent(1), "1%")
        XCTAssertEqual(FleetText.percent(42.4), "42%")
        XCTAssertEqual(FleetText.percent(42.5), "43%")
        XCTAssertEqual(FleetText.degrees(61.6), "62°C")
    }

    /// A worker sent to another machine belongs to that machine's count.
    func testRunningWorkersLeaveOutOnesSentElsewhere() throws {
        let snapshot = try XCTUnwrap(FleetSnapshot.decode(line: Data(sample.utf8)))
        XCTAssertEqual(snapshot.runningWorkers.map(\.id), ["1"])
    }

    /// Shell profiles print things. They must not count as a report.
    func testIgnoresOtherOutput() {
        XCTAssertNil(FleetSnapshot.decode(line: Data("Welcome to Ubuntu".utf8)))
    }

    func testCommandsKeepPythonIsolatedAndUseSudoOnlyWhenAsked() {
        let local = FleetConnection.command(for: FleetDevice(id: "m", label: "m"), alias: nil)
        XCTAssertEqual(local.arguments, ["python3", "-I", "-u", "-", "watch"])

        let plain = FleetDevice(id: "p", label: "p", ssh: ["box"])
        XCTAssertEqual(FleetConnection.command(for: plain, alias: "box").arguments.last,
                       "python3 -I -u - watch")

        let other = FleetDevice(id: "c", label: "c", ssh: ["box"], sudoUser: "someone")
        XCTAssertEqual(FleetConnection.command(for: other, alias: "box").arguments.last,
                       "sudo -n -u 'someone' -H python3 -I -u - watch")
    }

    func testRetryBacksOffToAMinute() {
        XCTAssertEqual(FleetConnection.retryDelay(afterFailures: 1), 5)
        XCTAssertEqual(FleetConnection.retryDelay(afterFailures: 2), 10)
        XCTAssertEqual(FleetConnection.retryDelay(afterFailures: 20), 60)
    }

    func testReasonSkipsRoutineSSHWarning() {
        let errors = Data("ssh: connect to host x port 22: Operation timed out\nWarning: Permanently added 'x' to known hosts.\n".utf8)
        XCTAssertEqual(FleetConnection.reason(from: errors),
                       "ssh: connect to host x port 22: Operation timed out")
    }

    func testMissingConfigMeansThisMac() {
        XCTAssertEqual(FleetConfig.load(from: "/nonexistent/fleet.json"), [FleetConfig.thisMac])
    }

    // MARK: Dispatcher in a snapshot

    private func line(dispatcher: String) -> Data {
        Data((String(sample.dropLast()) + ",\"dispatcher\":" + dispatcher + "}").utf8)
    }

    private func dispatcherJSON(_ rules: FleetRules = .defaults, version: Int = 1) throws -> String {
        let encoded = String(decoding: try rules.encoded(), as: UTF8.self)
        return """
        {"installed":true,"version":\(version),"claudeInstalled":true,"rules":\(encoded),"rulesValid":true,\
        "providers":{"minimax":{"configured":true,"used":2,"cooling":false,"coolingReason":""},\
        "claude":{"configured":false,"used":0,"cooling":true,"coolingReason":"429"}}}
        """
    }

    func testAnOlderAgentHasNoDispatcher() throws {
        XCTAssertNil(try XCTUnwrap(FleetSnapshot.decode(line: Data(sample.utf8))).dispatcher)
    }

    func testDecodesDispatcher() throws {
        let snapshot = try XCTUnwrap(FleetSnapshot.decode(line: line(dispatcher: dispatcherJSON())))
        let dispatcher = try XCTUnwrap(snapshot.dispatcher)
        XCTAssertEqual(dispatcher.rules, .defaults)
        XCTAssertEqual(dispatcher.status, .upToDate)
        XCTAssertEqual(dispatcher.providers["minimax"]?.used, 2)
        XCTAssertEqual(dispatcher.providers["claude"]?.coolingReason, "429")
    }

    /// Losing the whole line would look like a dead connection.
    func testAnUnreadableDispatcherDoesNotLoseTheReport() throws {
        for bad in ["null", "{\"installed\":\"nope\"}", "[]"] {
            let snapshot = try XCTUnwrap(FleetSnapshot.decode(line: line(dispatcher: bad)), bad)
            XCTAssertNil(snapshot.dispatcher, bad)
            XCTAssertEqual(snapshot.host.hostname, "box")
        }
    }

    func testDispatcherStatus() {
        XCTAssertEqual(FleetDispatcherStatus(installed: false, version: nil), .notInstalled)
        XCTAssertEqual(FleetDispatcherStatus(installed: true, version: 0), .older(version: 0))
        XCTAssertEqual(FleetDispatcherStatus(installed: true, version: nil), .older(version: 0))
        XCTAssertEqual(FleetDispatcherStatus(installed: true, version: 1, bundled: 2), .older(version: 1))
        XCTAssertEqual(FleetDispatcherStatus(installed: true, version: FleetRules.bundledDispatcherVersion), .upToDate)
        XCTAssertEqual(FleetDispatcherStatus(installed: true, version: 2, bundled: 1), .newer(version: 2))
        XCTAssertTrue(FleetDispatcherStatus.notInstalled.offersInstall)
        XCTAssertTrue(FleetDispatcherStatus.older(version: 0).offersInstall)
        XCTAssertFalse(FleetDispatcherStatus.upToDate.offersInstall)
        XCTAssertFalse(FleetDispatcherStatus.newer(version: 2).offersInstall)
    }

    // MARK: Rules

    private let documentedRules = """
    {"version":1,"revision":0,"balance":"proportional","order":["minimax","zhipu","claude"],\
    "providers":{"minimax":{"enabled":true,"slots":8},"zhipu":{"enabled":true,"slots":4},\
    "claude":{"enabled":true,"slots":4},"qwen":{"enabled":false,"slots":4}},\
    "levels":{"simple":{"minimax":"fast","zhipu":"flash","claude":"sonnet"},\
    "normal":{"minimax":"m3","zhipu":"flash","claude":"sonnet"},"hard":{"claude":"claude-opus-5-5"}},\
    "defaults":{"timeoutSeconds":900,"cooldownSeconds":1800}}
    """

    func testDefaultsMatchTheDocument() throws {
        let documented = try JSONDecoder().decode(FleetRules.self, from: Data(documentedRules.utf8))
        XCTAssertEqual(documented, .defaults)
        XCTAssertEqual(FleetRules.defaults.validate(), [])
    }

    func testRulesKeepTheirShapeWithStableKeys() throws {
        let encoded = try FleetRules.defaults.encoded()
        XCTAssertEqual(encoded, try FleetRules.defaults.encoded())
        XCTAssertTrue(String(decoding: encoded, as: UTF8.self).hasPrefix(
            "{\"balance\":\"proportional\",\"defaults\":{\"cooldownSeconds\":1800,\"timeoutSeconds\":900},\"levels\":"))
        let written = try JSONSerialization.jsonObject(with: encoded) as? NSDictionary
        let documented = try JSONSerialization.jsonObject(with: Data(documentedRules.utf8)) as? NSDictionary
        XCTAssertEqual(written, documented)
        XCTAssertEqual(try JSONDecoder().decode(FleetRules.self, from: encoded), .defaults)
    }

    private func issues(_ change: (inout FleetRules) -> Void) -> [FleetRules.Issue] {
        var rules = FleetRules.defaults
        change(&rules)
        return rules.validate()
    }

    func testValidationOfEachRule() {
        XCTAssertEqual(issues { $0.version = 2 }, [.version])
        XCTAssertEqual(issues { $0.revision = -1 }, [.revision])
        XCTAssertEqual(issues { $0.balance = "random" }, [.balance])
        XCTAssertEqual(issues { $0.providers["nope"] = .init(enabled: false, slots: 1) }, [.unknownProvider("nope")])
        XCTAssertEqual(issues { $0.providers["minimax"]?.slots = 0 }, [.slots(provider: "minimax")])
        XCTAssertEqual(issues { $0.providers["minimax"]?.slots = 33 }, [.slots(provider: "minimax")])
        XCTAssertEqual(issues { $0.providers["minimax"]?.slots = 32 }, [])
        XCTAssertEqual(issues { $0.levels["hard"] = nil }, [.missingLevel("hard")])
        XCTAssertEqual(issues { $0.levels["hard"] = [:] }, [.emptyLevel("hard")])
        XCTAssertEqual(issues { $0.levels["extra"] = ["claude": "sonnet"] }, [.unknownLevel("extra")])
        XCTAssertEqual(issues { $0.levels["hard"]?["claude"] = "fast" }, [.model(level: "hard", provider: "claude")])
        XCTAssertEqual(issues { $0.levels["hard"]?["nope"] = "x" }, [.levelProvider(level: "hard", provider: "nope")])
        XCTAssertEqual(issues { $0.defaults.timeoutSeconds = 29 }, [.timeout])
        XCTAssertEqual(issues { $0.defaults.timeoutSeconds = 86401 }, [.timeout])
        XCTAssertEqual(issues { $0.defaults.cooldownSeconds = -1 }, [.cooldown])
        XCTAssertEqual(issues { $0.defaults.cooldownSeconds = 86401 }, [.cooldown])
        XCTAssertEqual(issues { $0.defaults.cooldownSeconds = 0 }, [])
        XCTAssertEqual(FleetRules.Issue.slots(provider: "zhipu").field, "providers.zhipu.slots")
    }

    /// The document's own default leaves the disabled `qwen` out of `order`.
    func testOrderRules() {
        XCTAssertEqual(issues { $0.order = ["minimax", "zhipu"] }, [.order])
        XCTAssertEqual(issues { $0.order = ["minimax", "zhipu", "claude", "claude"] }, [.order])
        XCTAssertEqual(issues { $0.order = ["minimax", "zhipu", "claude", "nope"] }, [.order])
        XCTAssertEqual(issues { $0.order = ["qwen", "claude", "zhipu", "minimax"] }, [])
        XCTAssertEqual(FleetRules.defaults.normalized.order, ["minimax", "zhipu", "claude", "qwen"])
    }

    func testEditingKeepsRulesConsistent() {
        var rules = FleetRules.defaults
        rules.setEnabled(true, for: "qwen")
        XCTAssertEqual(rules.order.last, "qwen")
        XCTAssertEqual(rules.validate(), [])

        rules = .defaults
        rules.move("qwen", by: -1)
        XCTAssertEqual(rules.order, ["minimax", "zhipu", "qwen", "claude"])
        rules = .defaults
        rules.move("minimax", by: -1)
        XCTAssertEqual(rules, .defaults)

        rules = .defaults
        rules.setModel(nil, level: "hard", provider: "claude")
        XCTAssertEqual(rules.validate(), [.emptyLevel("hard")])

        rules = .defaults
        rules.providers["qwen"] = nil
        rules.setSlots(6, for: "qwen")
        XCTAssertEqual(rules.providers["qwen"]?.slots, 6)
        XCTAssertEqual(rules.validate(), [])

        rules = .defaults
        rules.setEnabled(false, for: "claude")
        XCTAssertEqual(rules.levelsWithoutEnabledProvider(), ["hard"])
    }

    // MARK: Unsaved edits

    private let moment = Date(timeIntervalSince1970: 1000)

    private func rules(revision: Int, balance: String = FleetRules.balanceProportional) -> FleetRules {
        var rules = FleetRules.defaults
        rules.revision = revision
        rules.balance = balance
        return rules
    }

    func testACleanEditorFollowsReports() {
        var editor = FleetRulesEditor(rules: rules(revision: 5), valid: true)
        let newer = rules(revision: 6, balance: FleetRules.balancePriority)
        editor.receive(rules: newer, valid: true, now: moment)
        XCTAssertEqual(editor.draft, newer)
        XCTAssertFalse(editor.isDirty)
    }

    /// Saving with the newest revision would overwrite the change it just saw.
    func testAReportKeepsUnsavedEditsAndASaveStillConflicts() {
        var editor = FleetRulesEditor(rules: rules(revision: 5), valid: true)
        editor.draft.defaults.timeoutSeconds = 100
        let mine = editor.draft
        let theirs = rules(revision: 6, balance: FleetRules.balancePriority)
        editor.receive(rules: theirs, valid: true, now: moment)
        XCTAssertEqual(editor.draft, mine)
        XCTAssertTrue(editor.changedElsewhere)
        XCTAssertEqual(editor.expectedRevision, 5)
        _ = editor.beginSave()
        editor.conflicted(fileRevision: 6)
        XCTAssertEqual(editor.draft, theirs)
        XCTAssertFalse(editor.isDirty)
        XCTAssertFalse(editor.isSaving)
    }

    func testSavingIgnoresReportsAlreadyOnTheirWay() {
        var editor = FleetRulesEditor(rules: rules(revision: 5), valid: true)
        editor.draft.defaults.timeoutSeconds = 100
        let sent = editor.beginSave()
        XCTAssertEqual(sent.revision, 5)
        XCTAssertTrue(editor.isSaving)
        XCTAssertFalse(editor.canSave)
        editor.receive(rules: rules(revision: 5), valid: true, now: moment)
        XCTAssertEqual(editor.draft.defaults.timeoutSeconds, 100)

        editor.saved(sent, revision: 6, now: moment)
        XCTAssertFalse(editor.isDirty)
        editor.receive(rules: rules(revision: 5), valid: true, now: moment.addingTimeInterval(3))
        XCTAssertEqual(editor.draft.revision, 6)
        XCTAssertEqual(editor.draft.defaults.timeoutSeconds, 100)
        // A deleted file really does go back to 0.
        editor.receive(rules: rules(revision: 0), valid: true, now: moment.addingTimeInterval(60))
        XCTAssertEqual(editor.draft.revision, 0)
    }

    func testASaveWhoseAnswerWasLostSettlesWhenItShowsUp() {
        var editor = FleetRulesEditor(rules: rules(revision: 5), valid: true)
        editor.draft.defaults.timeoutSeconds = 100
        _ = editor.beginSave()
        editor.isSaving = false
        var landed = editor.draft
        landed.revision = 6
        editor.receive(rules: landed, valid: true, now: moment)
        XCTAssertFalse(editor.isDirty)
        XCTAssertFalse(editor.changedElsewhere)
    }

    /// An invalid file reports the defaults at revision 0, whatever it holds, so
    /// without this it could never be replaced from the app.
    func testAnInvalidFileUsesTheRevisionFromAConflict() {
        var editor = FleetRulesEditor(rules: .defaults, valid: false)
        editor.draft.defaults.timeoutSeconds = 100
        _ = editor.beginSave()
        editor.conflicted(fileRevision: 9)
        XCTAssertEqual(editor.expectedRevision, 9)
        editor.receive(rules: .defaults, valid: false, now: moment)
        XCTAssertEqual(editor.expectedRevision, 9)
        editor.receive(rules: rules(revision: 10), valid: true, now: moment)
        XCTAssertEqual(editor.expectedRevision, 10)
    }

    func testResetDropsEdits() {
        var editor = FleetRulesEditor(rules: rules(revision: 6), valid: true)
        editor.draft.balance = FleetRules.balancePriority
        editor.isSaving = true
        editor.wasReset(revision: 7, now: moment)
        XCTAssertEqual(editor.draft.revision, 7)
        XCTAssertFalse(editor.isDirty)
        XCTAssertEqual(editor.draft.balance, FleetRules.balanceProportional)
    }

    // MARK: Commands

    func testRulesSetCommandCarriesBase64() throws {
        let action = try FleetConnection.rulesSetAction(.defaults, expectedRevision: 3)
        XCTAssertEqual(action[0], "rules-set")
        XCTAssertEqual(Data(base64Encoded: action[1]), try FleetRules.defaults.encoded())
        XCTAssertEqual(action[2], "3")

        let box = FleetDevice(id: "p", label: "p", ssh: ["box"])
        XCTAssertEqual(FleetConnection.command(for: box, alias: "box", action: action).arguments.last,
                       "python3 -I -u - 'rules-set' '\(action[1])' '3'")
        let local = FleetConnection.command(for: FleetDevice(id: "m", label: "m"), alias: nil, action: action)
        XCTAssertEqual(local.arguments, ["python3", "-I", "-u", "-", "rules-set", action[1], "3"])
        XCTAssertEqual(FleetConnection.rulesResetAction(expectedRevision: 4), ["rules-reset", "4"])
    }

    func testInstallCommandIsTheDocumentsAndNeverForAReadOnlyAccount() throws {
        let exact = FleetConnection.installShellCommand(byteCount: 123)
        XCTAssertTrue(exact.contains("= 123 ]"))
        XCTAssertTrue(exact.hasSuffix("mv ~/.local/bin/.mm-worker.tmp ~/.local/bin/mm-worker"))
        let box = FleetDevice(id: "p", label: "p", ssh: ["box"])
        let remote = try XCTUnwrap(FleetConnection.installCommand(for: box, alias: "box", byteCount: 123))
        XCTAssertEqual(remote.path, "/usr/bin/ssh")
        XCTAssertEqual(remote.arguments.last, exact)
        XCTAssertTrue(remote.arguments.contains("BatchMode=yes"))
        let local = try XCTUnwrap(FleetConnection.installCommand(for: FleetDevice(id: "m", label: "m"), alias: nil, byteCount: 123))
        XCTAssertEqual(local.path, "/bin/sh")
        XCTAssertEqual(local.arguments, ["-c", exact])
        let other = FleetDevice(id: "c", label: "c", ssh: ["box"], sudoUser: "someone")
        XCTAssertNil(FleetConnection.installCommand(for: other, alias: "box", byteCount: 123))
    }

    private func result(_ output: String, status: Int32 = 0) -> FleetConnection.CommandResult {
        var result = FleetConnection.CommandResult()
        result.status = status
        result.output = Data(output.utf8)
        result.inputComplete = true
        return result
    }

    func testRulesAnswers() {
        XCTAssertEqual(FleetConnection.rulesAnswer(from: result("noise\n{\"ok\": true, \"revision\": 4}\n")),
                       .saved(revision: 4))
        XCTAssertEqual(FleetConnection.rulesAnswer(from: result("{\"ok\": false, \"error\": \"conflict\", \"revision\": 9}")),
                       .conflict(revision: 9))
        XCTAssertEqual(FleetConnection.rulesAnswer(from: result("{\"ok\": false, \"error\": \"bad slots\"}")),
                       .failed(.refused("bad slots")))
        XCTAssertEqual(FleetConnection.rulesAnswer(from: result("")), .failed(.noAnswer))
        XCTAssertEqual(FleetConnection.rulesAnswer(from: result("{\"ok\": true}")), .failed(.noAnswer))
    }

    /// Success takes a zero status, the time limit not hit, and all input taken.
    func testACommandOnlySucceedsOnACleanFinish() {
        XCTAssertNil(result("").failure)
        XCTAssertEqual(FleetConnection.rulesAnswer(from: result("{\"ok\": true, \"revision\": 4}", status: 1)),
                       .failed(.exit(1, "")))
        var timedOut = result("{\"ok\": true, \"revision\": 4}")
        timedOut.timedOut = true
        XCTAssertEqual(FleetConnection.rulesAnswer(from: timedOut), .failed(.timedOut))
        var cut = result("")
        cut.inputComplete = false
        XCTAssertEqual(cut.failure, .inputIncomplete)
    }

    // MARK: Totals

    private func state(_ id: String, link: FleetLink, rules: FleetRules?) throws -> FleetDeviceState {
        let data = try rules.map { line(dispatcher: try dispatcherJSON($0)) } ?? Data(sample.utf8)
        return FleetDeviceState(device: FleetDevice(id: id, label: id, ssh: ["x"]), link: link,
                                snapshot: FleetSnapshot.decode(line: data))
    }

    /// A machine set up to use both minimax and claude.
    private func keyed(_ id: String, link: FleetLink, rules: FleetRules?) throws -> FleetDeviceState {
        var result = try state(id, link: link, rules: rules)
        result.snapshot?.dispatcher?.providers["claude"]?.configured = true
        return result
    }

    func testSlotsOnlyCountWhereTheyCouldBeUsed() throws {
        var noKey = try keyed("a", link: .live(via: "x"), rules: .defaults)
        noKey.snapshot?.dispatcher?.providers["minimax"]?.configured = false
        var notInstalled = try keyed("b", link: .live(via: "x"), rules: .defaults)
        notInstalled.snapshot?.dispatcher?.installed = false
        var readOnly = try keyed("c", link: .live(via: "x"), rules: .defaults)
        readOnly.device.sudoUser = "someone"
        let totals = FleetProviderTotal.compute(devices: [noKey, notInstalled, readOnly],
                                                limits: ["minimax": 1])
        let minimax = try XCTUnwrap(totals.first { $0.provider == "minimax" })
        XCTAssertEqual(minimax.slots, 0)
        XCTAssertFalse(minimax.exceedsLimit)
        XCTAssertEqual(totals.first { $0.provider == "claude" }?.slots, 4)
    }

    func testProviderTotals() throws {
        var disabled = FleetRules.defaults
        disabled.providers["minimax"]?.enabled = false
        let totals = FleetProviderTotal.compute(devices: [
            try keyed("a", link: .live(via: "x"), rules: .defaults),
            try keyed("b", link: .offline(reason: ""), rules: .defaults),
            try keyed("c", link: .live(via: "x"), rules: disabled),
            try keyed("d", link: .live(via: "x"), rules: nil),
        ], limits: ["minimax": 8, "claude": 12])
        XCTAssertEqual(totals.map(\.provider), FleetRules.providerNames)

        let minimax = try XCTUnwrap(totals.first { $0.provider == "minimax" })
        // One running worker on each live machine: a stale count stays out.
        XCTAssertEqual(minimax.running, 3)
        // An offline machine's rules still hold. A disabled provider takes no work.
        XCTAssertEqual(minimax.slots, 16)
        XCTAssertTrue(minimax.exceedsLimit)

        // Exactly at the limit is fine, and no limit means no warning.
        XCTAssertEqual(totals.first { $0.provider == "claude" }?.slots, 12)
        XCTAssertEqual(totals.first { $0.provider == "claude" }?.exceedsLimit, false)
        XCTAssertEqual(totals.first { $0.provider == "zhipu" }?.exceedsLimit, false)
    }

    // MARK: Account limits in fleet.json

    private func temporaryConfig() throws -> String {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: folder) }
        return folder.appendingPathComponent("fleet.json").path
    }

    func testSettingALimitKeepsEverythingElse() throws {
        let path = try temporaryConfig()
        let original = """
        {"devices":[{"id":"p","label":"Box","ssh":["a","b"],"future":1}],"theme":{"x":[1,2]},"accountLimits":{"zhipu":4}}
        """
        try Data(original.utf8).write(to: URL(fileURLWithPath: path))
        XCTAssertEqual(FleetConfig.load(from: path).map(\.id), ["p"])

        try FleetConfig.setAccountLimit(8, provider: "minimax", at: path)
        XCTAssertEqual(FleetConfig.loadAccountLimits(from: path), ["zhipu": 4, "minimax": 8])
        let written = try JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: path))) as? NSDictionary
        let wanted = try JSONSerialization.jsonObject(with: Data("""
        {"devices":[{"id":"p","label":"Box","ssh":["a","b"],"future":1}],"theme":{"x":[1,2]},"accountLimits":{"zhipu":4,"minimax":8}}
        """.utf8)) as? NSDictionary
        XCTAssertEqual(written, wanted)

        try FleetConfig.setAccountLimit(nil, provider: "minimax", at: path)
        try FleetConfig.setAccountLimit(nil, provider: "zhipu", at: path)
        let cleared = try JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: path))) as? NSDictionary
        XCTAssertNil(cleared?["accountLimits"])
        XCTAssertNotNil(cleared?["devices"])
    }

    func testConfigWithoutLimitsStillDecodes() throws {
        let path = try temporaryConfig()
        try Data("{\"devices\":[{\"id\":\"p\",\"label\":\"Box\"}]}".utf8).write(to: URL(fileURLWithPath: path))
        XCTAssertEqual(FleetConfig.load(from: path).map(\.id), ["p"])
        XCTAssertEqual(FleetConfig.loadAccountLimits(from: path), [:])
        XCTAssertEqual(FleetConfig.loadAccountLimits(from: "/nonexistent/fleet.json"), [:])
    }

    func testOnlyPositiveNumbersAreLimits() throws {
        let path = try temporaryConfig()
        try Data("{\"accountLimits\":{\"minimax\":\"lots\",\"zhipu\":0,\"claude\":true,\"qwen\":6}}".utf8)
            .write(to: URL(fileURLWithPath: path))
        XCTAssertEqual(FleetConfig.loadAccountLimits(from: path), ["qwen": 6])
    }

    func testAnUnreadableConfigIsNotOverwritten() throws {
        let path = try temporaryConfig()
        try Data("{not json".utf8).write(to: URL(fileURLWithPath: path))
        XCTAssertThrowsError(try FleetConfig.setAccountLimit(3, provider: "qwen", at: path))
        XCTAssertEqual(FileManager.default.contents(atPath: path), Data("{not json".utf8))
    }

    func testALimitCanCreateTheFile() throws {
        let path = try temporaryConfig()
        try FleetConfig.setAccountLimit(3, provider: "qwen", at: path)
        XCTAssertEqual(FleetConfig.loadAccountLimits(from: path), ["qwen": 3])
        XCTAssertEqual(FleetConfig.load(from: path), [FleetConfig.thisMac])
    }
}
