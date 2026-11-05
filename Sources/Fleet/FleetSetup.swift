import Foundation

/// One change to a machine's rules that a proposal suggests.
enum FleetSetupChange: Equatable, Hashable {
    case disableProvider(String)
    case setCLI(String)
    case addToLevel(level: String, provider: String, model: String)
}

/// Something only the user can do: sign-ins and keys are never automated.
enum FleetSetupNeed: Equatable, Hashable {
    case signIn(tool: String)   // installed, and signedIn == false
    case addKey                 // no provider left that can work
}

/// What setting up one machine would do. Nothing in it is applied until the
/// user confirms.
struct FleetSetupProposal: Equatable {
    var installDispatcher: Bool
    var installRunner: Bool
    var changes: [FleetSetupChange]
    var rules: FleetRules?      // the whole rules to write, nil if `changes` is empty
    var needs: [FleetSetupNeed]
    var isEmpty: Bool { !installDispatcher && !installRunner && changes.isEmpty && needs.isEmpty }
}

/// Works out a machine's proposal from its snapshot. Pure, so every rule of the
/// contract can be tested without a machine.
enum FleetSetup {
    /// The tools a sign-in is asked for, in the order they are listed.
    static let toolNames = ["claude", "opencode", "codex"]

    /// Nil if the snapshot lacks `tools` (an older agent), so nothing can be said.
    static func proposal(for snapshot: FleetSnapshot,
                         bundledDispatcher: Int = FleetRules.bundledDispatcherVersion) -> FleetSetupProposal? {
        guard let tools = snapshot.tools else { return nil }
        var proposal = FleetSetupProposal(installDispatcher: false, installRunner: false,
                                          changes: [], rules: nil, needs: [])
        for tool in toolNames {
            if let found = tools[tool], found.installed, found.signedIn == false {
                proposal.needs.append(.signIn(tool: tool))
            }
        }
        // An unreadable dispatcher says nothing about what is installed or which
        // keys exist, so only the sign-ins can be said.
        guard let dispatcher = snapshot.dispatcher else { return proposal }

        proposal.installDispatcher = FleetDispatcherStatus(installed: dispatcher.installed,
                                                           version: dispatcher.version,
                                                           bundled: bundledDispatcher).offersInstall
        proposal.installRunner = dispatcher.remoteRunner == false

        var finalRules = dispatcher.rules
        // Invalid rules are reported as the defaults, which say nothing about the
        // file, so nothing is proposed for them.
        if dispatcher.rulesValid {
            let (changes, rules) = ruleChanges(dispatcher: dispatcher, tools: tools)
            // What doesn't validate would be refused anyway, so propose none of it.
            if !changes.isEmpty, rules.validate().isEmpty {
                proposal.changes = changes
                proposal.rules = rules
                finalRules = rules
            }
        }
        if !finalRules.providers.keys.contains(where: {
            finalRules.isEnabled($0) && !cannotWork($0, rules: finalRules, dispatcher: dispatcher)
        }) {
            proposal.needs.append(.addKey)
        }
        return proposal
    }

    /// Rules 3 to 5 of the contract, in order, and the rules they lead to.
    private static func ruleChanges(dispatcher: FleetDispatcher,
                                    tools: [String: FleetTool]) -> ([FleetSetupChange], FleetRules) {
        var rules = dispatcher.rules
        var changes: [FleetSetupChange] = []

        // Only ever towards opencode: claude may be installed and still unusable,
        // as on a machine in a blocked region, so its presence proves nothing.
        if tools["claude"]?.installed == false, tools["opencode"]?.installed == true,
           rules.cli != "opencode" {
            rules.cli = "opencode"
            changes.append(.setCLI("opencode"))
        }

        // A provider the user disabled is already off and is left alone.
        for provider in rules.displayOrder where rules.isEnabled(provider) {
            guard cannotWork(provider, rules: rules, dispatcher: dispatcher) else { continue }
            let enabledCount = rules.providers.values.filter(\.enabled).count
            // Keep the last one: with none enabled nothing could run at all, and
            // `.addKey` says what is missing instead.
            guard enabledCount > 1 else { continue }
            rules.setEnabled(false, for: provider)
            changes.append(.disableProvider(provider))
        }

        for level in rules.levelsWithoutEnabledProvider() {
            guard let provider = rules.order.first(where: { rules.isEnabled($0) }),
                  let model = defaultModel(provider: provider, level: level)
            else { continue }
            rules.setModel(model, level: level, provider: provider)
            changes.append(.addToLevel(level: level, provider: provider, model: model))
        }
        return (changes, rules)
    }

    /// A provider whose status isn't reported is not called broken: only a
    /// missing key, or claude under opencode, which has nothing to log in with.
    private static func cannotWork(_ provider: String, rules: FleetRules,
                                   dispatcher: FleetDispatcher) -> Bool {
        if dispatcher.providers[provider]?.configured == false { return true }
        return provider == "claude" && rules.cli == "opencode"
    }

    /// The model a provider gets when added to a level that has none.
    static func defaultModel(provider: String, level: String) -> String? {
        switch provider {
        case "minimax": return "m3"
        case "zhipu", "qwen": return "flash"
        case "claude": return level == "hard" ? "claude-opus-5-5" : "sonnet"
        default: return nil
        }
    }
}
