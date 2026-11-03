import SwiftUI

/// Words for rules and commands, in the user's language.
enum FleetRulesText {
    static func provider(_ name: String, _ language: AppLanguage) -> String {
        language.t("fleet.provider.\(name)")
    }

    static func level(_ name: String, _ language: AppLanguage) -> String {
        language.t("fleet.rules.level.\(name)")
    }

    static func issue(_ issue: FleetRules.Issue, _ language: AppLanguage) -> String {
        switch issue {
        case .version: return language.t("fleet.rules.issue.version")
        case .revision: return language.t("fleet.rules.issue.revision")
        case .balance: return language.t("fleet.rules.issue.balance")
        case .order: return language.t("fleet.rules.issue.order")
        case .unknownProvider(let provider):
            return language.t("fleet.rules.issue.unknownProvider", provider)
        case .slots(let provider):
            return language.t("fleet.rules.issue.slots", self.provider(provider, language),
                              FleetRules.slotRange.lowerBound, FleetRules.slotRange.upperBound)
        case .missingLevel(let level):
            return language.t("fleet.rules.issue.missingLevel", self.level(level, language))
        case .unknownLevel(let level):
            return language.t("fleet.rules.issue.unknownLevel", level)
        case .emptyLevel(let level):
            return language.t("fleet.rules.issue.emptyLevel", self.level(level, language))
        case .levelProvider(let level, let provider):
            return language.t("fleet.rules.issue.levelProvider", self.level(level, language), provider)
        case .model(let level, let provider):
            return language.t("fleet.rules.issue.model", self.level(level, language),
                              self.provider(provider, language))
        case .timeout:
            return language.t("fleet.rules.issue.timeout",
                              FleetRules.timeoutRange.lowerBound, FleetRules.timeoutRange.upperBound)
        case .cooldown:
            return language.t("fleet.rules.issue.cooldown",
                              FleetRules.cooldownRange.lowerBound, FleetRules.cooldownRange.upperBound)
        }
    }

    static func failure(_ failure: FleetCommandFailure, _ language: AppLanguage) -> String {
        switch failure {
        case .timedOut: return language.t("fleet.rules.failure.timedOut")
        case .noAnswer: return language.t("fleet.rules.failure.noAnswer")
        case .exit(let status, let reason):
            return reason.isEmpty
                ? language.t("fleet.rules.failure.exit", Int(status))
                : language.t("fleet.rules.failure.exitReason", Int(status), reason)
        case .launch(let reason): return language.t("fleet.rules.failure.launch", reason)
        case .refused(let reason): return language.t("fleet.rules.failure.refused", reason)
        case .readOnly: return language.t("fleet.rules.readOnly")
        case .inputIncomplete: return language.t("fleet.rules.failure.input")
        case .unavailable: return language.t("fleet.rules.install.unavailable")
        }
    }

    static func notice(_ notice: FleetRulesNotice, _ language: AppLanguage) -> String {
        switch notice {
        case .conflict: return language.t("fleet.rules.conflict")
        case .saved: return language.t("fleet.rules.saved")
        case .saveFailed(let reason):
            return language.t("fleet.rules.saveFailed", failure(reason, language))
        case .installed: return language.t("fleet.rules.install.done")
        case .installFailed(let reason):
            return language.t("fleet.rules.install.failed", failure(reason, language))
        }
    }

    static func status(_ status: FleetDispatcherStatus, _ language: AppLanguage) -> String {
        switch status {
        case .notInstalled: return language.t("fleet.rules.status.notInstalled")
        case .older(let version):
            return version == 0
                ? language.t("fleet.rules.status.shell")
                : language.t("fleet.rules.status.older", version)
        case .upToDate: return language.t("fleet.rules.status.upToDate")
        case .newer(let version): return language.t("fleet.rules.status.newer", version)
        }
    }

    /// The text under a provider's name: its key, and cooling if it is.
    static func providerDetail(_ name: String, status: FleetProviderStatus?,
                               _ language: AppLanguage) -> String {
        guard let status else { return language.t("fleet.rules.key.unknown") }
        if status.configured { return language.t("fleet.rules.key.configured") }
        return language.t(name == "claude" ? "fleet.rules.key.noClaude" : "fleet.rules.key.missing")
    }
}

/// A digits-only field that reports as you type, so validation is live and a
/// click on Save can't miss a value still being edited. Nil means empty or too
/// long to be a number.
struct FleetNumberField: View {
    let value: Int?
    var placeholder = ""
    var width: CGFloat = 70
    let onChange: (Int?) -> Void
    @State private var text = ""

    var body: some View {
        TextField(placeholder, text: $text)
            .textFieldStyle(.roundedBorder)
            .multilineTextAlignment(.trailing)
            .font(.body.monospacedDigit())
            .frame(width: width)
            .labelsHidden()
            .onAppear { text = value.map(String.init) ?? "" }
            .onChange(of: text) { _, new in
                let digits = new.filter { $0.isASCII && $0.isNumber }
                if digits != new {
                    text = digits
                } else if Int(digits) != value {
                    onChange(Int(digits))
                }
            }
            // Revert, or a new report. Leaves alone what is being typed.
            .onChange(of: value) { _, new in
                if Int(text) != new { text = new.map(String.init) ?? "" }
            }
    }
}

// MARK: - Device page

/// The "Dispatch rules" section of a device's page.
struct FleetRulesSection: View {
    let state: FleetDeviceState
    let dispatcher: FleetDispatcher?
    @ObservedObject var store: FleetStore
    let language: AppLanguage
    @State private var confirmingInstall = false
    @State private var confirmingReset = false

    /// Shown for a typed number outside any range, which the fields report as nil.
    private static let invalidNumber = -1

    private var editor: FleetRulesEditor? { store.rulesEditors[state.id] }
    private var canEdit: Bool { store.canEditRules(on: state) && editor?.isSaving != true }

    var body: some View {
        FleetSection(title: language.t("fleet.rules.title")) {
            statusRow
            if let dispatcher, let editor {
                Divider()
                form(dispatcher, editor)
            } else {
                Text(language.t("fleet.rules.notReported"))
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.vertical, 8)
            }
        }
        .alert(language.t(installTitleKey), isPresented: $confirmingInstall) {
            Button(language.t(installButtonKey)) { store.installDispatcher(on: state) }
            Button(language.t("fleet.rules.cancel"), role: .cancel) {}
        } message: {
            Text(language.t("fleet.rules.install.message"))
        }
        .alert(language.t("fleet.rules.reset.title"), isPresented: $confirmingReset) {
            Button(language.t("fleet.rules.reset.confirm"), role: .destructive) {
                store.resetRules(on: state)
            }
            Button(language.t("fleet.rules.cancel"), role: .cancel) {}
        } message: {
            Text(language.t("fleet.rules.reset.message"))
        }
    }

    private var status: FleetDispatcherStatus? { dispatcher?.status }

    private var installTitleKey: String {
        status == .notInstalled ? "fleet.rules.install.titleNew" : "fleet.rules.install.titleUpdate"
    }

    private var installButtonKey: String {
        status == .notInstalled ? "fleet.rules.install.button" : "fleet.rules.install.update"
    }

    // MARK: Status

    @ViewBuilder
    private var statusRow: some View {
        if let status {
            HStack(spacing: 10) {
                Image(systemName: status.offersInstall ? "exclamationmark.circle.fill" : "checkmark.circle.fill")
                    .foregroundStyle(status.offersInstall ? Color.orange : Color.green)
                    .frame(width: 16)
                Text(FleetRulesText.status(status, language))
                Spacer(minLength: 8)
                if store.installing.contains(state.id) {
                    ProgressView()
                        .controlSize(.small)
                    Text(language.t("fleet.rules.install.running"))
                        .font(.callout)
                        .foregroundStyle(.secondary)
                } else if status.offersInstall {
                    if store.hasDispatcherFile {
                        Button(language.t(installButtonKey)) { confirmingInstall = true }
                            .disabled(!store.canInstall(on: state))
                    } else {
                        Text(language.t("fleet.rules.install.unavailable"))
                            .font(.callout)
                            .foregroundStyle(.secondary)
                    }
                }
            }
            .padding(.vertical, 8)
        }
    }

    // MARK: Form

    @ViewBuilder
    private func form(_ dispatcher: FleetDispatcher, _ editor: FleetRulesEditor) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            availability(dispatcher, editor)
            providers(dispatcher, editor.draft)
            Divider()
            levels(editor.draft)
            Divider()
            options(editor.draft)
            footer(editor)
        }
        .padding(.vertical, 10)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// Why the form can't be used right now, or something about what it shows.
    @ViewBuilder
    private func availability(_ dispatcher: FleetDispatcher, _ editor: FleetRulesEditor) -> some View {
        if state.device.sudoUser != nil {
            note(language.t("fleet.rules.readOnly"), symbol: "lock.fill", tint: .secondary)
        } else if !state.isLive {
            note(language.t(editor.isDirty ? "fleet.rules.offlineUnsaved" : "fleet.rules.offline"),
                 symbol: "wifi.slash", tint: .secondary)
        }
        if !dispatcher.rulesValid {
            note(language.t("fleet.rules.invalid"), symbol: "exclamationmark.triangle.fill", tint: .orange)
        }
        if editor.changedElsewhere {
            note(language.t("fleet.rules.changedElsewhere"), symbol: "exclamationmark.triangle.fill", tint: .orange)
        }
    }

    private func note(_ text: String, symbol: String, tint: Color) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: symbol)
                .foregroundStyle(tint)
                .frame(width: 16)
            Text(text)
                .font(.callout)
                .foregroundStyle(tint == .secondary ? Color.secondary : tint)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func heading(_ key: String) -> some View {
        Text(language.t(key))
            .font(.callout.weight(.medium))
    }

    // MARK: Providers

    private func providers(_ dispatcher: FleetDispatcher, _ rules: FleetRules) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            heading("fleet.rules.providers")
            ForEach(FleetRules.providerNames, id: \.self) { name in
                providerRow(name, status: dispatcher.providers[name], rules: rules)
            }
            if FleetRules.providerNames.contains(where: { dispatcher.providers[$0]?.configured != true }) {
                Text(language.t("fleet.rules.keysNote"))
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private func providerRow(_ name: String, status: FleetProviderStatus?, rules: FleetRules) -> some View {
        let slots = rules.slots(name)
        return HStack(spacing: 10) {
            Toggle(isOn: Binding(
                get: { rules.isEnabled(name) },
                set: { value in store.editRules(on: state) { $0.setEnabled(value, for: name) } }
            )) { EmptyView() }
                .toggleStyle(.switch)
                .controlSize(.small)
                .labelsHidden()
                .disabled(!canEdit)
            VStack(alignment: .leading, spacing: 1) {
                Text(FleetRulesText.provider(name, language))
                    .lineLimit(1)
                HStack(spacing: 4) {
                    Text(FleetRulesText.providerDetail(name, status: status, language))
                        .foregroundStyle(status?.configured == true ? Color.secondary : Color.orange)
                    if state.isLive, let status, status.cooling {
                        Text(verbatim: "·")
                            .foregroundStyle(.secondary)
                        Text(status.coolingReason.isEmpty
                             ? language.t("fleet.rules.cooling")
                             : language.t("fleet.rules.coolingReason", status.coolingReason))
                            .foregroundStyle(.orange)
                            .help(status.coolingReason)
                    }
                }
                .font(.caption)
                .lineLimit(1)
            }
            Spacer(minLength: 8)
            // As elsewhere, a live reading isn't shown for a machine that's gone.
            Text(verbatim: "\(state.isLive ? String(status?.used ?? 0) : FleetText.unknown) / \(slots)")
                .font(.body.monospacedDigit())
                .foregroundStyle(.secondary)
            Stepper(value: Binding(
                get: { slots },
                set: { value in store.editRules(on: state) { $0.setSlots(value, for: name) } }
            ), in: FleetRules.slotRange) { EmptyView() }
                .labelsHidden()
                .disabled(!canEdit)
                .help(language.t("fleet.rules.slotsHelp"))
        }
        .padding(.vertical, 2)
    }

    // MARK: Levels

    private func levels(_ rules: FleetRules) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            heading("fleet.rules.levels")
            Grid(alignment: .leading, horizontalSpacing: 8, verticalSpacing: 6) {
                GridRow {
                    Color.clear.frame(width: 1, height: 1)
                    ForEach(FleetRules.providerNames, id: \.self) { provider in
                        Text(FleetRulesText.provider(provider, language))
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
                ForEach(FleetRules.levelNames, id: \.self) { level in
                    GridRow {
                        Text(FleetRulesText.level(level, language))
                        ForEach(FleetRules.providerNames, id: \.self) { provider in
                            modelMenu(level: level, provider: provider, rules: rules)
                        }
                    }
                }
            }
            modelListStatus(rules)
        }
        .task(id: "\(state.isLive)-\(store.acceptsFullModelNames(on: state))") {
            store.loadModels(on: state)
        }
    }

    /// Where the menus' models came from, and what went wrong getting them.
    @ViewBuilder
    private func modelListStatus(_ rules: FleetRules) -> some View {
        if !store.acceptsFullModelNames(on: state) {
            note(language.t("fleet.rules.models.needsUpdate"))
        } else {
            HStack(spacing: 8) {
                note(language.t(store.modelListFailed.contains(state.id)
                                ? "fleet.rules.models.failed" : "fleet.rules.models.source"))
                Button(language.t("fleet.rules.models.refresh")) { store.loadModels(on: state, again: true) }
                    .controlSize(.small)
                    .disabled(!canEdit || store.loadingModels.contains(state.id))
                if store.loadingModels.contains(state.id) {
                    ProgressView().controlSize(.small)
                }
            }
            // A provider that is switched off has no need of a list.
            ForEach(FleetRules.providerNames.filter {
                rules.providers[$0]?.enabled == true && store.modelListings[state.id]?.errors[$0] != nil
            }, id: \.self) { provider in
                note(language.t("fleet.rules.models.providerFailed", FleetRulesText.provider(provider, language),
                                store.modelListings[state.id]?.errors[provider] ?? ""))
            }
        }
    }

    private func note(_ text: String) -> some View {
        Text(verbatim: text)
            .font(.caption)
            .foregroundStyle(.secondary)
    }

    private func modelMenu(level: String, provider: String, rules: FleetRules) -> some View {
        let models = FleetRules.menuModels(provider: provider,
                                           listed: store.modelListings[state.id]?.models[provider] ?? [],
                                           current: rules.model(level: level, provider: provider),
                                           acceptsFullNames: store.acceptsFullModelNames(on: state))
        return Picker(selection: Binding<String?>(
            get: { rules.model(level: level, provider: provider) },
            set: { value in store.editRules(on: state) { $0.setModel(value, level: level, provider: provider) } }
        )) {
            Text(language.t("fleet.rules.dontUse")).tag(String?.none)
            Divider()
            ForEach(models, id: \.id) { model in
                Text(verbatim: model.displayName).tag(String?.some(model.id))
            }
        } label: { EmptyView() }
            .pickerStyle(.menu)
            .labelsHidden()
            .frame(maxWidth: .infinity)
            .disabled(!canEdit)
            // A narrow window cuts "MiniMax-M2.7" and "MiniMax-M2.7-highspeed" alike.
            .help(models
                .first { $0.id == rules.model(level: level, provider: provider) }?.displayName ?? "")
    }

    // MARK: Balance, order, timings

    private func options(_ rules: FleetRules) -> some View {
        HStack(alignment: .top, spacing: 24) {
            VStack(alignment: .leading, spacing: 6) {
                heading("fleet.rules.balance")
                Picker(selection: Binding(
                    get: { rules.balance },
                    set: { value in store.editRules(on: state) { $0.balance = value } }
                )) {
                    ForEach(FleetRules.balances, id: \.self) { balance in
                        Text(language.t("fleet.rules.balance.\(balance)")).tag(balance)
                    }
                } label: { EmptyView() }
                    .pickerStyle(.segmented)
                    .labelsHidden()
                    .fixedSize()
                    .disabled(!canEdit)
                Text(language.t(rules.balance == FleetRules.balancePriority
                                ? "fleet.rules.balance.priorityHelp"
                                : "fleet.rules.balance.proportionalHelp"))
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: 240, alignment: .leading)
                timing("fleet.rules.timeout", value: rules.defaults.timeoutSeconds) { value in
                    store.editRules(on: state) { $0.defaults.timeoutSeconds = value }
                }
                .padding(.top, 8)
                timing("fleet.rules.cooldown", value: rules.defaults.cooldownSeconds) { value in
                    store.editRules(on: state) { $0.defaults.cooldownSeconds = value }
                }
            }
            VStack(alignment: .leading, spacing: 4) {
                heading("fleet.rules.order")
                ForEach(Array(rules.displayOrder.enumerated()), id: \.element) { index, name in
                    orderRow(name, index: index, count: rules.displayOrder.count)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private func timing(_ key: String, value: Int, set: @escaping (Int) -> Void) -> some View {
        HStack(spacing: 6) {
            Text(language.t(key))
                .frame(minWidth: 110, alignment: .leading)
            FleetNumberField(value: value < 0 ? nil : value) { set($0 ?? Self.invalidNumber) }
                .disabled(!canEdit)
            Text(language.t("fleet.rules.seconds"))
                .foregroundStyle(.secondary)
        }
    }

    private func orderRow(_ name: String, index: Int, count: Int) -> some View {
        HStack(spacing: 6) {
            Text(verbatim: "\(index + 1).")
                .font(.body.monospacedDigit())
                .foregroundStyle(.secondary)
                .frame(width: 18, alignment: .trailing)
            Text(FleetRulesText.provider(name, language))
                .frame(minWidth: 80, alignment: .leading)
            Button {
                store.editRules(on: state) { $0.move(name, by: -1) }
            } label: { Image(systemName: "chevron.up") }
                .disabled(!canEdit || index == 0)
                .help(language.t("fleet.rules.moveUp"))
            Button {
                store.editRules(on: state) { $0.move(name, by: 1) }
            } label: { Image(systemName: "chevron.down") }
                .disabled(!canEdit || index == count - 1)
                .help(language.t("fleet.rules.moveDown"))
        }
        .controlSize(.small)
    }

    // MARK: Footer

    @ViewBuilder
    private func footer(_ editor: FleetRulesEditor) -> some View {
        let issues = editor.issues
        let stranded = editor.draft.levelsWithoutEnabledProvider()
        Divider()
        if !issues.isEmpty {
            VStack(alignment: .leading, spacing: 3) {
                ForEach(issues, id: \.self) { issue in
                    note(FleetRulesText.issue(issue, language),
                         symbol: "xmark.circle.fill", tint: .red)
                }
            }
        }
        ForEach(stranded, id: \.self) { level in
            note(language.t("fleet.rules.levelStranded", FleetRulesText.level(level, language)),
                 symbol: "exclamationmark.triangle.fill", tint: .orange)
        }
        if let notice = store.rulesNotices[state.id] {
            noticeRow(notice)
        }
        Text(language.t("fleet.rules.appliesNote"))
            .font(.caption)
            .foregroundStyle(.secondary)
            .fixedSize(horizontal: false, vertical: true)
        HStack(spacing: 8) {
            Button(language.t("fleet.rules.resetButton")) { confirmingReset = true }
                .disabled(!canEdit)
            Spacer(minLength: 8)
            if editor.isSaving {
                ProgressView()
                    .controlSize(.small)
                Text(language.t("fleet.rules.saving"))
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
            Button(language.t("fleet.rules.revert")) { store.revertRules(on: state) }
                .disabled(!editor.isDirty || editor.isSaving)
            Button(language.t("fleet.rules.save")) { store.saveRules(on: state) }
                .buttonStyle(.borderedProminent)
                .disabled(!editor.canSave || !store.canEditRules(on: state))
        }
    }

    private func noticeRow(_ notice: FleetRulesNotice) -> some View {
        let isGood = notice == .saved || notice == .installed
        return note(FleetRulesText.notice(notice, language),
                    symbol: isGood ? "checkmark.circle.fill" : "exclamationmark.triangle.fill",
                    tint: isGood ? .green : .orange)
    }
}

// MARK: - Overview

/// Each provider added up across machines, against what the account allows.
struct FleetProviderTotalsSection: View {
    @ObservedObject var store: FleetStore
    let language: AppLanguage

    var body: some View {
        let totals = store.providerTotals
        VStack(alignment: .leading, spacing: 8) {
            FleetSection(title: language.t("fleet.totals.title")) {
                Grid(alignment: .leading, horizontalSpacing: 16, verticalSpacing: 8) {
                    GridRow {
                        Color.clear.frame(width: 1, height: 1)
                        column("fleet.totals.running")
                        column("fleet.totals.slots")
                        column("fleet.totals.limit")
                    }
                    ForEach(totals) { total in
                        GridRow {
                            Text(FleetRulesText.provider(total.provider, language))
                                .frame(maxWidth: .infinity, alignment: .leading)
                            number(total.running, tint: .primary)
                            number(total.slots, tint: total.exceedsLimit ? .orange : .primary)
                            FleetNumberField(value: total.limit,
                                             placeholder: language.t("fleet.totals.noLimit")) { value in
                                store.setAccountLimit(value, provider: total.provider)
                            }
                        }
                    }
                }
                .padding(.vertical, 8)
            }
            ForEach(totals.filter(\.exceedsLimit)) { total in
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Image(systemName: "exclamationmark.triangle.fill")
                    Text(language.t("fleet.totals.warning",
                                    FleetRulesText.provider(total.provider, language),
                                    total.slots, total.limit ?? 0))
                        .fixedSize(horizontal: false, vertical: true)
                }
                .font(.callout)
                .foregroundStyle(.orange)
            }
        }
        .alert(language.t("fleet.totals.writeFailed"), isPresented: $store.accountLimitFailed) {
            Button(language.t("fleet.ok"), role: .cancel) {}
        }
    }

    private func column(_ key: String) -> some View {
        Text(language.t(key))
            .font(.caption)
            .foregroundStyle(.secondary)
            .gridColumnAlignment(.trailing)
    }

    private func number(_ value: Int, tint: Color) -> some View {
        Text(verbatim: "\(value)")
            .font(.body.monospacedDigit())
            .foregroundStyle(tint)
    }
}
