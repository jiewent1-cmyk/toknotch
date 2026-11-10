import XCTest
@testable import TokNotch

final class FleetSetupTests: XCTestCase {
    private let bundled = FleetRules.bundledDispatcherVersion

    private func tool(_ installed: Bool, signedIn: Bool? = true) -> FleetTool {
        FleetTool(installed: installed, hasData: installed, lastUsed: installed ? 1_793_865_706 : nil,
                  signedIn: signedIn)
    }

    /// Claude only, unless told otherwise.
    private func tools(claude: Bool = true, opencode: Bool = false, codex: Bool = false) -> [String: FleetTool] {
        ["claude": tool(claude), "opencode": tool(opencode), "codex": tool(codex)]
    }

    /// A machine with an up-to-date dispatcher and runner. `configured` names the
    /// providers with a key, claude among them where it is installed, as the agent says.
    private func machine(tools: [String: FleetTool]?,
                         rules: FleetRules = .defaults,
                         configured: Set<String> = ["minimax", "zhipu", "claude"],
                         installed: Bool = true,
                         version: Int? = FleetRules.bundledDispatcherVersion,
                         remoteRunner: Bool? = true,
                         rulesValid: Bool = true) -> FleetSnapshot {
        var statuses: [String: FleetProviderStatus] = [:]
        for provider in FleetRules.providerNames {
            statuses[provider] = FleetProviderStatus(configured: configured.contains(provider), used: 0,
                                                     cooling: false, coolingReason: "")
        }
        let dispatcher = FleetDispatcher(installed: installed, version: version,
                                         claudeInstalled: tools?["claude"]?.installed ?? true,
                                         rules: rules, rulesValid: rulesValid, providers: statuses,
                                         remoteRunner: remoteRunner)
        return FleetSnapshot(agentVersion: 6,
                             host: FleetSnapshot.Host(hostname: "box", system: "Linux", machine: "x86_64", cpus: 4),
                             sessions: [], todayByModel: [:], workers: [],
                             dispatcher: dispatcher, claudeAccount: nil, usageHistory: nil, tools: tools)
    }

    private func proposal(_ snapshot: FleetSnapshot) throws -> FleetSetupProposal {
        try XCTUnwrap(FleetSetup.proposal(for: snapshot))
    }

    /// What the machine reports once the proposal has been applied.
    private func applied(_ snapshot: FleetSnapshot, _ proposal: FleetSetupProposal) -> FleetSnapshot {
        var next = snapshot
        if proposal.installDispatcher {
            next.dispatcher?.installed = true
            next.dispatcher?.version = bundled
        }
        if proposal.installRunner { next.dispatcher?.remoteRunner = true }
        if let rules = proposal.rules {
            next.dispatcher?.rules = rules
            next.dispatcher?.rules.revision += 1
        }
        return next
    }

    /// Applying a proposal and asking again must leave nothing to change.
    private func assertIdempotent(_ snapshot: FleetSnapshot, file: StaticString = #filePath, line: UInt = #line) throws {
        let first = try proposal(snapshot)
        let second = try proposal(applied(snapshot, first))
        XCTAssertFalse(second.installDispatcher, file: file, line: line)
        XCTAssertFalse(second.installRunner, file: file, line: line)
        XCTAssertEqual(second.changes, [], file: file, line: line)
        XCTAssertNil(second.rules, file: file, line: line)
        XCTAssertEqual(second.needs, first.needs, file: file, line: line)
    }

    // MARK: Decoding

    private let head = """
    {"agentVersion":6,"host":{"hostname":"box","system":"Linux","machine":"x86_64","cpus":4},\
    "sessions":[],"todayByModel":{},"workers":[]
    """

    func testDecodesToolsAndRunner() throws {
        let rules = String(decoding: try FleetRules.defaults.encoded(), as: UTF8.self)
        let json = head + #","tools":{"claude":{"installed":true,"hasData":true,"lastUsed":1793865706.5,"signedIn":true},"#
            + #""codex":{"installed":false,"hasData":false,"lastUsed":null,"signedIn":null}},"#
            + #""dispatcher":{"installed":true,"version":4,"claudeInstalled":true,"rules":"# + rules
            + #","rulesValid":true,"providers":{},"remoteRunner":false}}"#
        let snapshot = try XCTUnwrap(FleetSnapshot.decode(line: Data(json.utf8)))
        XCTAssertEqual(snapshot.tools?["claude"], FleetTool(installed: true, hasData: true,
                                                            lastUsed: 1793865706.5, signedIn: true))
        XCTAssertEqual(snapshot.tools?["codex"], FleetTool(installed: false, hasData: false,
                                                           lastUsed: nil, signedIn: nil))
        XCTAssertEqual(snapshot.dispatcher?.remoteRunner, false)
    }

    func testAnOlderAgentHasNoToolsOrRunner() throws {
        let rules = String(decoding: try FleetRules.defaults.encoded(), as: UTF8.self)
        let json = head + #","dispatcher":{"installed":true,"version":4,"claudeInstalled":true,"rules":"# + rules
            + #","rulesValid":true,"providers":{}}}"#
        let snapshot = try XCTUnwrap(FleetSnapshot.decode(line: Data(json.utf8)))
        XCTAssertNil(snapshot.tools)
        XCTAssertNotNil(snapshot.dispatcher)
        XCTAssertNil(snapshot.dispatcher?.remoteRunner)
        XCTAssertNil(FleetSetup.proposal(for: snapshot))
    }

    /// Losing the whole line would look like a dead connection.
    func testUnreadableToolsDoNotLoseTheReport() throws {
        for bad in ["null", "[]", #"{"claude":{"installed":"yes"}}"#] {
            let json = head + #","tools":"# + bad + "}"
            let snapshot = try XCTUnwrap(FleetSnapshot.decode(line: Data(json.utf8)), bad)
            XCTAssertNil(snapshot.tools, bad)
            XCTAssertEqual(snapshot.host.hostname, "box")
        }
    }

    // MARK: Rules 1 and 2

    func testAnOlderAgentGivesNoProposal() {
        XCTAssertNil(FleetSetup.proposal(for: machine(tools: nil)))
    }

    func testAFullySetUpMachineHasNothingToDo() throws {
        let result = try proposal(machine(tools: tools()))
        XCTAssertTrue(result.isEmpty)
        XCTAssertNil(result.rules)
    }

    func testInstallsAMissingOrOlderDispatcherOnly() throws {
        XCTAssertTrue(try proposal(machine(tools: tools(), installed: false, version: nil)).installDispatcher)
        XCTAssertTrue(try proposal(machine(tools: tools(), version: 0)).installDispatcher)
        XCTAssertTrue(try proposal(machine(tools: tools(), version: bundled - 1)).installDispatcher)
        XCTAssertFalse(try proposal(machine(tools: tools(), version: bundled)).installDispatcher)
        XCTAssertFalse(try proposal(machine(tools: tools(), version: bundled + 1)).installDispatcher,
                       "installing from here would be a downgrade")
        let later = try XCTUnwrap(FleetSetup.proposal(for: machine(tools: tools(), version: bundled),
                                                      bundledDispatcher: bundled + 1))
        XCTAssertTrue(later.installDispatcher)
    }

    func testInstallsTheRunnerOnlyWhenKnownToBeMissing() throws {
        XCTAssertTrue(try proposal(machine(tools: tools(), remoteRunner: false)).installRunner)
        XCTAssertFalse(try proposal(machine(tools: tools(), remoteRunner: true)).installRunner)
        XCTAssertFalse(try proposal(machine(tools: tools(), remoteRunner: nil)).installRunner,
                       "an older agent doesn't say")
    }

    // MARK: Rules 3 to 5

    /// Opencode without claude: the CLI moves, claude and the keyless go, and
    /// `hard`, which named only claude, gets the first provider left.
    func testAnOpencodeMachineMovesToOpencode() throws {
        let snapshot = machine(tools: tools(claude: false, opencode: true), configured: ["zhipu"])
        let result = try proposal(snapshot)
        XCTAssertEqual(result.changes, [
            .setCLI("opencode"),
            .disableProvider("minimax"),
            .disableProvider("claude"),
            .addToLevel(level: "hard", provider: "zhipu", model: "flash"),
        ])
        let rules = try XCTUnwrap(result.rules)
        XCTAssertEqual(rules.cli, "opencode")
        XCTAssertEqual(rules.levels["hard"], ["claude": "claude-opus-5-5", "zhipu": "flash"])
        XCTAssertEqual(rules.validate(), [])
        XCTAssertEqual(result.needs, [])
        try assertIdempotent(snapshot)
    }

    /// Claude may be installed and still unusable, as in a blocked region.
    func testNothingMovesTheCLIBackFromOpencode() throws {
        var rules = FleetRules.defaults
        rules.cli = "opencode"
        let snapshot = machine(tools: tools(claude: true, opencode: true), rules: rules)
        let result = try proposal(snapshot)
        XCTAssertEqual(result.changes, [
            .disableProvider("claude"),
            .addToLevel(level: "hard", provider: "minimax", model: "m3"),
        ])
        XCTAssertEqual(result.rules?.cli, "opencode")
        XCTAssertFalse(result.changes.contains(.setCLI("claude")))
        try assertIdempotent(snapshot)
    }

    func testClaudeInstalledLeavesTheCLIAlone() throws {
        let result = try proposal(machine(tools: tools(claude: true, opencode: true)))
        XCTAssertTrue(result.isEmpty)
    }

    func testAMachineWithOnlyClaudeDropsTheKeylessProviders() throws {
        let snapshot = machine(tools: tools(), configured: ["claude"])
        let result = try proposal(snapshot)
        XCTAssertEqual(result.changes, [.disableProvider("minimax"), .disableProvider("zhipu")])
        var expected = FleetRules.defaults
        expected.providers["minimax"]?.enabled = false
        expected.providers["zhipu"]?.enabled = false
        XCTAssertEqual(result.rules, expected, "levels keep their models, cli stays claude")
        XCTAssertEqual(result.needs, [])
        try assertIdempotent(snapshot)
    }

    func testAProviderTheUserDisabledStaysDisabled() throws {
        var rules = FleetRules.defaults
        rules.providers["zhipu"]?.enabled = false
        // qwen is off in the defaults and has a key here; zhipu is off and has none.
        let snapshot = machine(tools: tools(), rules: rules, configured: ["minimax", "claude", "qwen"])
        let result = try proposal(snapshot)
        XCTAssertTrue(result.isEmpty)
    }

    func testNothingIsEverEnabled() throws {
        var rules = FleetRules.defaults
        rules.providers["minimax"]?.enabled = false
        rules.providers["zhipu"]?.enabled = false
        // Only claude is on, and it can't work under opencode: it stays, alone.
        rules.cli = "opencode"
        let snapshot = machine(tools: tools(claude: true, opencode: true), rules: rules,
                               configured: ["minimax", "zhipu", "claude", "qwen"])
        let result = try proposal(snapshot)
        XCTAssertEqual(result.changes, [])
        XCTAssertNil(result.rules)
        XCTAssertEqual(result.needs, [.addKey])
    }

    /// Disabling every keyless provider would leave none, so the last stays on.
    func testTheLastEnabledProviderIsKept() throws {
        let snapshot = machine(tools: tools(claude: false), configured: [])
        let result = try proposal(snapshot)
        XCTAssertEqual(result.changes, [.disableProvider("minimax"), .disableProvider("zhipu")])
        let rules = try XCTUnwrap(result.rules)
        XCTAssertTrue(rules.isEnabled("claude"))
        XCTAssertEqual(result.needs, [.addKey])
        try assertIdempotent(snapshot)

        var one = FleetRules.defaults
        one.providers["zhipu"]?.enabled = false
        one.providers["claude"]?.enabled = false
        one.levels["hard"] = ["minimax": "m3"]
        let alone = try proposal(machine(tools: tools(), rules: one, configured: ["claude"]))
        XCTAssertEqual(alone.changes, [])
        XCTAssertNil(alone.rules)
        XCTAssertEqual(alone.needs, [.addKey])
    }

    /// The first enabled provider in `order` fills the level; claude gets Opus for `hard`.
    func testALevelWithoutAnEnabledProviderGetsTheFirstInOrder() throws {
        var rules = FleetRules.defaults
        rules.order = ["claude", "minimax", "zhipu"]
        rules.levels["hard"] = ["minimax": "m27"]
        rules.providers["minimax"]?.enabled = false
        let snapshot = machine(tools: tools(), rules: rules)
        let result = try proposal(snapshot)
        XCTAssertEqual(result.changes, [.addToLevel(level: "hard", provider: "claude", model: "claude-opus-5-5")])
        XCTAssertEqual(result.rules?.levels["hard"], ["minimax": "m27", "claude": "claude-opus-5-5"])
        try assertIdempotent(snapshot)
    }

    func testDefaultModels() {
        XCTAssertEqual(FleetSetup.defaultModel(provider: "minimax", level: "normal"), "m3")
        XCTAssertEqual(FleetSetup.defaultModel(provider: "zhipu", level: "hard"), "flash")
        XCTAssertEqual(FleetSetup.defaultModel(provider: "qwen", level: "simple"), "flash")
        XCTAssertEqual(FleetSetup.defaultModel(provider: "claude", level: "simple"), "sonnet")
        XCTAssertEqual(FleetSetup.defaultModel(provider: "claude", level: "hard"), "claude-opus-5-5")
        XCTAssertEqual(FleetSetup.defaultModel(provider: "codex", level: "hard"), "default")
        for provider in FleetRules.providerNames {
            for level in FleetRules.levelNames {
                let model = FleetSetup.defaultModel(provider: provider, level: level) ?? ""
                XCTAssertTrue(FleetRules.isAllowedModel(model, provider: provider), "\(provider) \(level)")
            }
        }
    }

    // MARK: Codex

    /// Codex signed in, and every level stranded: still nothing turns it on.
    func testSetupNeverEnablesCodex() throws {
        let everything: Set<String> = ["minimax", "zhipu", "claude", "qwen", "codex"]
        let plain = try proposal(machine(tools: tools(codex: true), configured: everything))
        XCTAssertTrue(plain.isEmpty)

        var rules = FleetRules.defaults
        rules.providers["minimax"]?.enabled = false
        rules.providers["zhipu"]?.enabled = false
        rules.providers["claude"]?.enabled = false
        let stranded = try proposal(machine(tools: tools(codex: true), rules: rules, configured: everything))
        XCTAssertEqual(stranded.changes, [])
        XCTAssertNil(stranded.rules)
        XCTAssertEqual(stranded.needs, [.addKey])

        // A file from before codex, as on real machines today.
        var before = FleetRules.defaults
        before.providers["codex"] = nil
        let old = machine(tools: tools(codex: true), rules: before, configured: everything)
        let untouched = try proposal(old)
        XCTAssertTrue(untouched.isEmpty)
        try assertIdempotent(old)
    }

    /// Off already, codex is left alone; turned on without a sign-in, it goes off once.
    func testCodexIsDisabledOnlyOnce() throws {
        let alreadyOff = try proposal(machine(tools: tools()))
        XCTAssertTrue(alreadyOff.isEmpty)

        var rules = FleetRules.defaults
        rules.setEnabled(true, for: "codex")
        let snapshot = machine(tools: tools(), rules: rules)
        let result = try proposal(snapshot)
        XCTAssertEqual(result.changes, [.disableProvider("codex")])
        var expected = rules
        expected.providers["codex"]?.enabled = false
        XCTAssertEqual(result.rules, expected)
        try assertIdempotent(snapshot)
    }

    /// Only once the user has turned codex on can it fill a level, with `default`.
    func testACodexTheUserEnabledCanFillALevel() throws {
        var rules = FleetRules.defaults
        rules.providers["claude"]?.enabled = false
        rules.providers["codex"]?.enabled = true
        rules.order = ["codex", "minimax", "zhipu", "claude"]
        let snapshot = machine(tools: tools(codex: true), rules: rules,
                               configured: ["minimax", "zhipu", "claude", "codex"])
        let result = try proposal(snapshot)
        XCTAssertEqual(result.changes, [.addToLevel(level: "hard", provider: "codex", model: "default")])
        XCTAssertEqual(result.rules?.levels["hard"], ["claude": "claude-opus-5-5", "codex": "default"])
        try assertIdempotent(snapshot)
    }

    /// Slots, balance, order, defaults, models and revision are never touched.
    func testOnlyTheProposedFieldsChange() throws {
        var rules = FleetRules.defaults
        rules.revision = 7
        rules.balance = FleetRules.balancePriority
        rules.order = ["zhipu", "claude", "minimax", "qwen"]
        rules.providers["zhipu"]?.slots = 2
        rules.providers["minimax"]?.slots = 16
        rules.levels["normal"] = ["minimax": "MiniMax-M2.5", "zhipu": "glm-5.1"]
        rules.defaults = FleetRules.Defaults(timeoutSeconds: 1200, cooldownSeconds: 60)
        let snapshot = machine(tools: tools(), rules: rules, configured: ["zhipu", "claude"])
        let result = try proposal(snapshot)
        XCTAssertEqual(result.changes, [.disableProvider("minimax")])
        var expected = rules
        expected.providers["minimax"]?.enabled = false
        XCTAssertEqual(result.rules, expected)
        try assertIdempotent(snapshot)
    }

    /// Invalid rules are reported as the defaults, which say nothing about the file.
    func testInvalidRulesGetNoRuleChanges() throws {
        let snapshot = machine(tools: tools(claude: false, opencode: true), configured: [],
                               remoteRunner: false, rulesValid: false)
        let result = try proposal(snapshot)
        XCTAssertEqual(result.changes, [])
        XCTAssertNil(result.rules)
        XCTAssertTrue(result.installRunner)
        XCTAssertEqual(result.needs, [.addKey])
    }

    /// What wouldn't validate would be refused, so none of it is proposed.
    func testRulesThatWouldNotValidateAreNotProposed() throws {
        var rules = FleetRules.defaults
        rules.defaults.timeoutSeconds = 5
        let result = try proposal(machine(tools: tools(), rules: rules, configured: ["claude"]))
        XCTAssertEqual(result.changes, [])
        XCTAssertNil(result.rules)
    }

    // MARK: Rule 6

    func testSignInIsAskedOnlyOfInstalledToolsThatAreSignedOut() throws {
        let found = [
            "codex": tool(true, signedIn: false),
            "opencode": tool(true, signedIn: nil),
            "claude": tool(true, signedIn: false),
        ]
        XCTAssertEqual(try proposal(machine(tools: found)).needs, [.signIn(tool: "claude"), .signIn(tool: "codex")])
        let notInstalled = ["claude": tool(true), "codex": tool(false, signedIn: false)]
        XCTAssertEqual(try proposal(machine(tools: notInstalled)).needs, [])
    }

    func testAddKeyIsAskedOnce() throws {
        let result = try proposal(machine(tools: tools(claude: false), configured: []))
        XCTAssertEqual(result.needs.filter { $0 == .addKey }.count, 1)
    }
}
