import Foundation

/// One machine's dispatch rules: `~/.config/mm-worker/rules.json`, as described
/// in `docs/FLEET-RULES.md`. Field names are the JSON's, so what is written here
/// has exactly the file's shape.
///
/// `balance` and the provider and level names stay plain strings, so a value
/// this build has never heard of is reported by `validate()` rather than making
/// the whole report undecodable.
struct FleetRules: Codable, Equatable {
    struct Provider: Codable, Equatable {
        var enabled: Bool
        var slots: Int
    }

    struct Defaults: Codable, Equatable {
        var timeoutSeconds: Int
        var cooldownSeconds: Int
    }

    var version: Int
    var revision: Int
    var balance: String
    var order: [String]
    var providers: [String: Provider]
    /// Level, then provider, then that provider's model at this level.
    var levels: [String: [String: String]]
    var defaults: Defaults

    /// The `MM_WORKER_VERSION` of the `mm-worker.py` in this app's bundle.
    static let bundledDispatcherVersion = 1

    static let balanceProportional = "proportional"
    static let balancePriority = "priority"
    static let balances = [balanceProportional, balancePriority]
    static let levelNames = ["simple", "normal", "hard"]
    static let providerNames = ["minimax", "zhipu", "claude", "qwen"]
    static let slotRange = 1...32
    static let timeoutRange = 30...86400
    static let cooldownRange = 0...86400

    struct Model: Equatable {
        var id: String
        var displayName: String
    }

    /// The models each provider may be given, in menu order.
    static let allowedModels: [String: [Model]] = [
        "minimax": [
            Model(id: "m3", displayName: "MiniMax-M3"),
            Model(id: "m27", displayName: "MiniMax-M2.7"),
            Model(id: "fast", displayName: "MiniMax-M2.7-highspeed"),
        ],
        "zhipu": [
            Model(id: "flash", displayName: "GLM-5.3-Flash"),
        ],
        "claude": [
            Model(id: "sonnet", displayName: "Sonnet"),
            Model(id: "haiku", displayName: "Haiku"),
            Model(id: "claude-opus-5-5", displayName: "Opus 5.5"),
        ],
        "qwen": [
            Model(id: "glm", displayName: "GLM-5.3"),
            Model(id: "max", displayName: "qwen3.8-max"),
            Model(id: "flash", displayName: "qwen3.8-flash"),
            Model(id: "lite", displayName: "qwen3.6-flash"),
            Model(id: "ds", displayName: "DeepSeek-V4-Pro-0813"),
        ],
    ]

    static let defaults = FleetRules(
        version: 1,
        revision: 0,
        balance: balanceProportional,
        order: ["minimax", "zhipu", "claude"],
        providers: [
            "minimax": Provider(enabled: true, slots: 8),
            "zhipu": Provider(enabled: true, slots: 4),
            "claude": Provider(enabled: true, slots: 4),
            "qwen": Provider(enabled: false, slots: 4),
        ],
        levels: [
            "simple": ["minimax": "fast", "zhipu": "flash", "claude": "sonnet"],
            "normal": ["minimax": "m3", "zhipu": "flash", "claude": "sonnet"],
            "hard": ["claude": "claude-opus-5-5"],
        ],
        defaults: Defaults(timeoutSeconds: 900, cooldownSeconds: 1800)
    )

    /// Sorted keys, so the same rules always give the same bytes.
    func encoded() throws -> Data {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        return try encoder.encode(self)
    }
}

// MARK: - Validation

extension FleetRules {
    /// One thing wrong with a set of rules, naming the field it is about.
    enum Issue: Hashable {
        case version
        case revision
        case balance
        case order
        case unknownProvider(String)
        case slots(provider: String)
        case missingLevel(String)
        case unknownLevel(String)
        case emptyLevel(String)
        case levelProvider(level: String, provider: String)
        case model(level: String, provider: String)
        case timeout
        case cooldown

        /// The field's path in the JSON.
        var field: String {
            switch self {
            case .version: return "version"
            case .revision: return "revision"
            case .balance: return "balance"
            case .order: return "order"
            case .unknownProvider(let provider): return "providers.\(provider)"
            case .slots(let provider): return "providers.\(provider).slots"
            case .missingLevel(let level), .unknownLevel(let level), .emptyLevel(let level):
                return "levels.\(level)"
            case .levelProvider(let level, let provider), .model(let level, let provider):
                return "levels.\(level).\(provider)"
            case .timeout: return "defaults.timeoutSeconds"
            case .cooldown: return "defaults.cooldownSeconds"
            }
        }
    }

    /// Everything the document calls invalid. Empty means these rules can be saved.
    func validate() -> [Issue] {
        var issues: [Issue] = []
        if version != 1 { issues.append(.version) }
        if revision < 0 { issues.append(.revision) }
        if !Self.balances.contains(balance) { issues.append(.balance) }
        if !isOrderValid { issues.append(.order) }
        for provider in providers.keys.sorted() {
            if !Self.providerNames.contains(provider) { issues.append(.unknownProvider(provider)) }
            if let slots = providers[provider]?.slots, !Self.slotRange.contains(slots) {
                issues.append(.slots(provider: provider))
            }
        }
        for level in Self.levelNames {
            guard let models = levels[level] else {
                issues.append(.missingLevel(level))
                continue
            }
            if models.isEmpty { issues.append(.emptyLevel(level)) }
            for provider in models.keys.sorted() {
                if providers[provider] == nil {
                    issues.append(.levelProvider(level: level, provider: provider))
                } else if !(Self.allowedModels[provider] ?? []).contains(where: { $0.id == models[provider] }) {
                    issues.append(.model(level: level, provider: provider))
                }
            }
        }
        for level in levels.keys.sorted() where !Self.levelNames.contains(level) {
            issues.append(.unknownLevel(level))
        }
        if !Self.timeoutRange.contains(defaults.timeoutSeconds) { issues.append(.timeout) }
        if !Self.cooldownRange.contains(defaults.cooldownSeconds) { issues.append(.cooldown) }
        return issues
    }

    /// Each named provider once, all of them known, and none that is enabled left
    /// out. The document says `order` holds exactly the providers in `providers`,
    /// but its own default leaves out `qwen`, which is disabled, so that much has
    /// to pass. `normalized` sends the full list either way.
    private var isOrderValid: Bool {
        Set(order).count == order.count
            && order.allSatisfy { providers[$0] != nil }
            && providers.allSatisfy { !$0.value.enabled || order.contains($0.key) }
    }

    /// These rules with `order` naming every provider, which satisfies the strict
    /// reading of the document. A disabled provider's place changes nothing.
    var normalized: FleetRules {
        var rules = self
        rules.order += providers.keys.sorted().filter { !order.contains($0) }
        return rules
    }

    /// Levels that are valid but can't get automatic work, because every provider
    /// they name is switched off. Worth a warning rather than an error.
    func levelsWithoutEnabledProvider() -> [String] {
        Self.levelNames.filter { level in
            guard let models = levels[level], !models.isEmpty else { return false }
            return !models.keys.contains { providers[$0]?.enabled == true }
        }
    }
}

// MARK: - Editing

extension FleetRules {
    /// A file may leave a provider out, as long as `order` agrees. The first
    /// change to one adds it, so the two stay consistent.
    private mutating func ensureProvider(_ provider: String) {
        guard providers[provider] == nil else { return }
        providers[provider] = Self.defaults.providers[provider] ?? Provider(enabled: false, slots: 4)
        if !order.contains(provider) { order.append(provider) }
    }

    func isEnabled(_ provider: String) -> Bool { providers[provider]?.enabled ?? false }

    func slots(_ provider: String) -> Int {
        providers[provider]?.slots ?? Self.defaults.providers[provider]?.slots ?? 4
    }

    mutating func setEnabled(_ enabled: Bool, for provider: String) {
        ensureProvider(provider)
        providers[provider]?.enabled = enabled
        if enabled, !order.contains(provider) { order.append(provider) }
    }

    mutating func setSlots(_ slots: Int, for provider: String) {
        ensureProvider(provider)
        providers[provider]?.slots = slots
    }

    func model(level: String, provider: String) -> String? { levels[level]?[provider] }

    /// Nil means "Don't use": the provider leaves this level.
    mutating func setModel(_ model: String?, level: String, provider: String) {
        if model != nil { ensureProvider(provider) }
        levels[level, default: [:]][provider] = model
    }

    /// Every provider, in preference order: the ones in `order`, then any left out.
    var displayOrder: [String] {
        order + Self.providerNames.filter { !order.contains($0) }
    }

    /// Moves a provider one place. Off either end does nothing.
    mutating func move(_ provider: String, by offset: Int) {
        var full = displayOrder
        guard let index = full.firstIndex(of: provider), full.indices.contains(index + offset)
        else { return }
        full.swapAt(index, index + offset)
        for name in full where providers[name] == nil { ensureProvider(name) }
        order = full
    }
}

// MARK: - What a machine reports

/// A provider as one machine sees it right now.
struct FleetProviderStatus: Codable, Equatable {
    /// Whether a key is present. The key itself never leaves the machine.
    var configured: Bool
    var used: Int
    var cooling: Bool
    var coolingReason: String
}

/// The `dispatcher` field of a snapshot.
struct FleetDispatcher: Codable, Equatable {
    var installed: Bool
    /// 0 is the older shell version. May be absent when nothing is installed.
    var version: Int?
    var claudeInstalled: Bool
    var rules: FleetRules
    var rulesValid: Bool
    var providers: [String: FleetProviderStatus]

    var status: FleetDispatcherStatus {
        FleetDispatcherStatus(installed: installed, version: version)
    }
}

/// How a machine's `mm-worker` compares with the one in this app.
enum FleetDispatcherStatus: Equatable {
    case notInstalled
    case older(version: Int)
    case upToDate
    /// Installed by a later TokNotch. Installing from here would be a downgrade.
    case newer(version: Int)

    init(installed: Bool, version: Int?, bundled: Int = FleetRules.bundledDispatcherVersion) {
        guard installed else {
            self = .notInstalled
            return
        }
        let version = version ?? 0
        if version < bundled {
            self = .older(version: version)
        } else if version > bundled {
            self = .newer(version: version)
        } else {
            self = .upToDate
        }
    }

    var offersInstall: Bool {
        switch self {
        case .notInstalled, .older: return true
        case .upToDate, .newer: return false
        }
    }
}

// MARK: - Unsaved edits

/// The rules being edited for one device. Pure, so what happens when a report
/// arrives in the middle of an edit can be tested without a window or a machine.
struct FleetRulesEditor: Equatable {
    /// What the edit started from. Its revision is what a save expects to find.
    private(set) var baseline: FleetRules
    var draft: FleetRules
    /// The newest rules the machine has reported.
    private(set) var latest: FleetRules
    private(set) var latestValid: Bool
    var isSaving = false
    /// The file's revision according to a conflict answer. See `expectedRevision`.
    private var fileRevision: Int?
    /// After a write, reports that were already on their way still show the
    /// old rules. Those are ignored for a moment rather than shown as a change.
    private var holdRevision: Int?
    private var holdUntil: Date?

    /// A report takes up to 5 seconds, so this covers those in flight. It has a
    /// limit because a deleted file really does go back to revision 0.
    static let holdSeconds: TimeInterval = 15

    init(rules: FleetRules, valid: Bool) {
        baseline = rules
        draft = rules
        latest = rules
        latestValid = valid
    }

    var isDirty: Bool { draft != baseline }
    var issues: [FleetRules.Issue] { draft.validate() }
    var canSave: Bool { isDirty && !isSaving && issues.isEmpty }
    /// Someone else wrote while this edit was open: saving will be refused.
    var changedElsewhere: Bool { isDirty && latest.revision != baseline.revision }

    /// The revision to send with a save. This is the one the edit started from
    /// rather than the newest one seen: using that would overwrite a change made
    /// meanwhile, which is exactly what the revision is there to prevent.
    ///
    /// The exception is an invalid file. Its reported rules are the defaults, at
    /// revision 0, which says nothing about the file. A conflict answer does, and
    /// without using it such a file could never be replaced from here.
    var expectedRevision: Int {
        if !latestValid, let fileRevision { return fileRevision }
        return baseline.revision
    }

    /// A new report. Never touches unsaved edits.
    mutating func receive(rules: FleetRules, valid: Bool, now: Date = Date()) {
        if let holdRevision, let holdUntil {
            if rules.revision < holdRevision, now < holdUntil { return }
            self.holdRevision = nil
            self.holdUntil = nil
        }
        latest = rules
        latestValid = valid
        if valid { fileRevision = nil }
        guard !isSaving else { return }
        var unversioned = draft
        unversioned.revision = rules.revision
        // Clean, or a save whose answer got lost and which did arrive.
        if !isDirty || unversioned == rules {
            baseline = rules
            draft = rules
        }
    }

    mutating func revert() {
        draft = baseline
    }

    /// Marks a save as under way and gives what to send.
    mutating func beginSave() -> FleetRules {
        isSaving = true
        draft = draft.normalized
        var submitted = draft
        submitted.revision = expectedRevision
        return submitted
    }

    /// The machine accepted `submitted`, which is now revision `revision`.
    mutating func saved(_ submitted: FleetRules, revision: Int, now: Date = Date()) {
        var rules = submitted
        rules.revision = revision
        baseline = rules
        draft.revision = revision
        latest = rules
        latestValid = true
        fileRevision = nil
        isSaving = false
        holdRevision = revision
        holdUntil = now.addingTimeInterval(Self.holdSeconds)
    }

    /// The defaults were written, replacing any unsaved edits.
    mutating func wasReset(revision: Int, now: Date = Date()) {
        draft = .defaults
        saved(.defaults, revision: revision, now: now)
    }

    /// Someone else got there first. Their rules win and these edits are dropped,
    /// which the caller tells the user. The next report brings theirs if the last
    /// one predates the write.
    mutating func conflicted(fileRevision: Int?) {
        self.fileRevision = fileRevision
        baseline = latest
        draft = latest
        isSaving = false
    }
}

// MARK: - Totals across machines

/// One provider added up over every machine.
struct FleetProviderTotal: Equatable, Identifiable {
    var provider: String
    /// Workers running right now, on machines that can currently say.
    var running: Int
    var slots: Int
    /// What the account allows, if the user has said.
    var limit: Int?

    var id: String { provider }
    var exceedsLimit: Bool { limit.map { slots > $0 } ?? false }

    /// Slots count only where the machine could really use them: a dispatcher is
    /// installed, the provider is enabled and has a key. Otherwise every machine's
    /// default rules would add up to a warning about work that can never run. An
    /// account read through someone else's sudo may use a different provider
    /// account, so its slots stay out too. Offline machines do count: they keep
    /// running work and their rules still hold. A running count from one is stale,
    /// so that is left out, as elsewhere in Fleet.
    static func compute(devices: [FleetDeviceState], limits: [String: Int]) -> [FleetProviderTotal] {
        FleetRules.providerNames.map { provider in
            var total = FleetProviderTotal(provider: provider, running: 0, slots: 0, limit: limits[provider])
            for state in devices {
                guard let snapshot = state.snapshot else { continue }
                if state.isLive {
                    total.running += snapshot.runningWorkers.filter { $0.provider == provider }.count
                }
                if let dispatcher = snapshot.dispatcher, dispatcher.installed,
                   state.device.sudoUser == nil,
                   dispatcher.providers[provider]?.configured == true,
                   let rule = dispatcher.rules.providers[provider], rule.enabled {
                    total.slots += rule.slots
                }
            }
            return total
        }
    }
}
