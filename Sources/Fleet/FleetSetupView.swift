import SwiftUI

/// Words for the Setup section, in the user's language.
enum FleetSetupText {
    /// The tools the agent reports, in the order they are shown.
    static let toolNames = ["claude", "opencode", "codex"]

    /// These strings exist in English and Chinese only, and `t` shows a missing
    /// key as itself, so any other language falls back to English here.
    static func t(_ language: AppLanguage, _ key: String, _ arguments: CVarArg...) -> String {
        var format = language.t(key)
        if format == key { format = AppLanguage.english.t(key) }
        return arguments.isEmpty ? format : String(format: format, locale: language.locale, arguments: arguments)
    }

    static func tool(_ name: String) -> String {
        switch name {
        case "claude": return "Claude Code"
        case "opencode": return "opencode"
        case "codex": return "Codex"
        default: return name
        }
    }

    static func model(_ model: String, provider: String) -> String {
        FleetRules.allowedModels[provider]?.first { $0.id == model }?.displayName ?? model
    }

    static func change(_ change: FleetSetupChange, _ language: AppLanguage) -> String {
        switch change {
        case .disableProvider(let provider):
            return FleetSetupText.t(language, "fleet.setup.change.disable", FleetRulesText.provider(provider, language))
        case .setCLI(let cli):
            return FleetSetupText.t(language, "fleet.setup.change.cli", cli)
        case .addToLevel(let level, let provider, let model):
            return FleetSetupText.t(language, "fleet.setup.change.level", FleetRulesText.provider(provider, language),
                              self.model(model, provider: provider), FleetRulesText.level(level, language))
        }
    }

    static func need(_ need: FleetSetupNeed, _ language: AppLanguage) -> String {
        switch need {
        case .signIn(let tool): return FleetSetupText.t(language, "fleet.setup.need.signIn", self.tool(tool))
        case .addKey: return FleetSetupText.t(language, "fleet.setup.need.addKey")
        }
    }

    /// Every step applying would take, one line each, as the confirmation lists them.
    static func steps(_ proposal: FleetSetupProposal, dispatcher: FleetDispatcher?,
                      _ language: AppLanguage) -> [String] {
        var lines: [String] = []
        if proposal.installDispatcher {
            lines.append(dispatcher?.status == .notInstalled
                         ? FleetSetupText.t(language, "fleet.setup.step.installDispatcher")
                         : FleetSetupText.t(language, "fleet.setup.step.updateDispatcher", FleetRules.bundledDispatcherVersion))
        }
        if proposal.installRunner { lines.append(FleetSetupText.t(language, "fleet.setup.step.installRunner")) }
        lines += proposal.changes.map { change($0, language) }
        return lines
    }

    static func notice(_ notice: FleetSetupNotice, _ language: AppLanguage) -> String {
        switch notice {
        case .applied: return FleetSetupText.t(language, "fleet.setup.applied")
        case .appliedRunnerKept: return FleetSetupText.t(language, "fleet.setup.appliedRunnerKept")
        case .conflict: return language.t("fleet.rules.conflict")
        case .dispatcherFailed(let failure):
            return FleetSetupText.t(language, "fleet.setup.failed.dispatcher", FleetRulesText.failure(failure, language))
        case .runnerFailed(let failure):
            return FleetSetupText.t(language, "fleet.setup.failed.runner", FleetRulesText.failure(failure, language))
        case .rulesFailed(let failure):
            return FleetSetupText.t(language, "fleet.setup.failed.rules", FleetRulesText.failure(failure, language))
        }
    }
}

/// The "Setup" section of a device's page: the tools found on the machine, and
/// what the app suggests for it. Nothing is applied without a confirmation.
struct FleetSetupSection: View {
    let state: FleetDeviceState
    let snapshot: FleetSnapshot
    @ObservedObject var store: FleetStore
    let language: AppLanguage
    @State private var confirming = false

    private var proposal: FleetSetupProposal? { store.setupProposal(on: state) }

    var body: some View {
        FleetSection(title: FleetSetupText.t(language, "fleet.setup.title")) {
            if let tools = snapshot.tools {
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(names(tools), id: \.self) { name in
                        if let tool = tools[name] { toolRow(name, tool) }
                    }
                }
                .padding(.vertical, 8)
                if let proposal {
                    Divider()
                    suggestions(proposal)
                }
            } else {
                Text(FleetSetupText.t(language, "fleet.setup.notReported"))
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.vertical, 8)
            }
        }
        .alert(FleetSetupText.t(language, "fleet.setup.confirm.title", state.device.label), isPresented: $confirming) {
            Button(FleetSetupText.t(language, "fleet.setup.confirm.apply")) { store.applySetup(on: state) }
            Button(language.t("fleet.rules.cancel"), role: .cancel) {}
        } message: {
            Text(verbatim: confirmMessage)
        }
    }

    /// The known tools first, in their order, then anything a newer agent adds.
    private func names(_ tools: [String: FleetTool]) -> [String] {
        FleetSetupText.toolNames.filter { tools[$0] != nil }
            + tools.keys.filter { !FleetSetupText.toolNames.contains($0) }.sorted()
    }

    private var confirmMessage: String {
        guard let proposal else { return "" }
        return FleetSetupText.steps(proposal, dispatcher: snapshot.dispatcher, language)
            .map { "• " + $0 }
            .joined(separator: "\n")
    }

    // MARK: Tools

    private func toolRow(_ name: String, _ tool: FleetTool) -> some View {
        HStack(spacing: 10) {
            Image(systemName: tool.installed ? "checkmark.circle.fill" : "minus.circle")
                .foregroundStyle(tool.installed ? Color.green : Color.secondary)
                .frame(width: 16)
            Text(verbatim: FleetSetupText.tool(name))
                .frame(minWidth: 100, alignment: .leading)
            Text(verbatim: toolDetail(tool))
                .font(.callout)
                .foregroundStyle(.secondary)
                .lineLimit(1)
            Spacer(minLength: 8)
            if tool.installed {
                signIn(tool.signedIn)
            }
        }
        .padding(.vertical, 2)
    }

    private func toolDetail(_ tool: FleetTool) -> String {
        guard tool.installed else { return FleetSetupText.t(language, "fleet.setup.tool.notInstalled") }
        guard let lastUsed = tool.lastUsed else { return FleetSetupText.t(language, "fleet.setup.tool.noData") }
        let when = Date(timeIntervalSince1970: lastUsed)
            .formatted(.relative(presentation: .named).locale(language.locale))
        return FleetSetupText.t(language, "fleet.setup.tool.lastUsed", when)
    }

    private func signIn(_ signedIn: Bool?) -> some View {
        let key: String
        let tint: Color
        switch signedIn {
        case .some(true):
            key = "fleet.setup.tool.signedIn"
            tint = .secondary
        case .some(false):
            key = "fleet.setup.tool.signedOut"
            tint = .orange
        case .none:
            key = "fleet.setup.tool.signInUnknown"
            tint = .secondary
        }
        return Text(FleetSetupText.t(language, key))
            .font(.callout)
            .foregroundStyle(tint)
    }

    // MARK: Suggestions

    @ViewBuilder
    private func suggestions(_ proposal: FleetSetupProposal) -> some View {
        let steps = FleetSetupText.steps(proposal, dispatcher: snapshot.dispatcher, language)
        VStack(alignment: .leading, spacing: 6) {
            if proposal.isEmpty {
                note(FleetSetupText.t(language, "fleet.setup.nothing"), symbol: "checkmark.circle.fill", tint: .green)
            }
            if !steps.isEmpty {
                Text(FleetSetupText.t(language, "fleet.setup.suggested"))
                    .font(.callout.weight(.medium))
                ForEach(steps, id: \.self) { step in
                    note(step, symbol: "arrow.right.circle", tint: .secondary)
                }
            }
            ForEach(proposal.needs, id: \.self) { need in
                note(FleetSetupText.need(need, language), symbol: "exclamationmark.circle.fill", tint: .orange)
            }
            if !steps.isEmpty {
                availability(proposal)
            }
            if let notice = store.setupNotices[state.id] {
                noticeRow(notice)
            }
            if !steps.isEmpty && state.isLive && state.device.sudoUser == nil {
                footer
            }
        }
        .padding(.vertical, 10)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// Why Apply isn't offered, or can't be used right now.
    @ViewBuilder
    private func availability(_ proposal: FleetSetupProposal) -> some View {
        if state.device.sudoUser != nil {
            note(FleetSetupText.t(language, "fleet.setup.readOnly"), symbol: "lock.fill", tint: .secondary)
        } else if !state.isLive {
            note(FleetSetupText.t(language, "fleet.setup.offline"), symbol: "wifi.slash", tint: .secondary)
        } else {
            if proposal.installDispatcher && !store.hasDispatcherFile {
                note(language.t("fleet.rules.install.unavailable"), symbol: "xmark.circle", tint: .secondary)
            }
            if proposal.installRunner && !store.hasRunnerFile {
                note(FleetSetupText.t(language, "fleet.setup.runnerUnavailable"), symbol: "xmark.circle", tint: .secondary)
            }
            if proposal.rules != nil && store.rulesEditors[state.id]?.isDirty == true {
                note(FleetSetupText.t(language, "fleet.setup.unsaved"), symbol: "exclamationmark.triangle.fill", tint: .orange)
            }
        }
    }

    private var footer: some View {
        HStack(spacing: 8) {
            Spacer(minLength: 8)
            if store.applyingSetup.contains(state.id) {
                ProgressView()
                    .controlSize(.small)
                Text(FleetSetupText.t(language, "fleet.setup.applying"))
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
            Button(FleetSetupText.t(language, "fleet.setup.apply")) { confirming = true }
                .buttonStyle(.borderedProminent)
                .disabled(!store.canApplySetup(on: state))
        }
    }

    private func note(_ text: String, symbol: String, tint: Color) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: symbol)
                .foregroundStyle(tint)
                .frame(width: 16)
            Text(verbatim: text)
                .font(.callout)
                .foregroundStyle(tint == .secondary ? Color.secondary : tint)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func noticeRow(_ notice: FleetSetupNotice) -> some View {
        let isGood = notice == .applied || notice == .appliedRunnerKept
        return note(FleetSetupText.notice(notice, language),
                    symbol: isGood ? "checkmark.circle.fill" : "exclamationmark.triangle.fill",
                    tint: isGood ? .green : .orange)
    }
}
