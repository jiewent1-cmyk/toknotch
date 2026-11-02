import Charts
import SwiftUI

/// The Usage page: where the fleet's tokens went, by machine, account or model.
struct FleetUsageView: View {
    static let maxWidth: CGFloat = 980

    @ObservedObject var store: FleetStore
    let language: AppLanguage
    /// Opens a device in the sidebar.
    let onSelect: (String) -> Void

    @AppStorage("fleetUsageRange") private var range: FleetUsageRange = .week
    @AppStorage("fleetUsageGrouping") private var grouping: FleetUsageGrouping = .machines
    @AppStorage("fleetUsageMetric") private var metric: FleetUsageMetric = .all
    @Environment(\.colorScheme) private var colorScheme
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var motion: Animation? { reduceMotion ? nil : .smooth(duration: 0.35) }

    var body: some View {
        let report = FleetUsageReport.compute(devices: store.devices, range: range,
                                              grouping: grouping, metric: metric)
        let palette = FleetUsagePalette(scheme: colorScheme)
        VStack(alignment: .leading, spacing: 32) {
            header
            if report.hasData {
                hero(report)
                if report.range == .today {
                    FleetUsageTodayBar(report: report, palette: palette, language: language)
                } else {
                    VStack(alignment: .leading, spacing: 16) {
                        FleetUsageChart(report: report, palette: palette, language: language)
                        if report.series.count >= 2 {
                            FleetUsageLegend(series: report.series, palette: palette, language: language)
                        }
                    }
                }
                FleetUsageLedger(report: report, devices: store.devices, palette: palette,
                                 language: language, onSelect: onSelect)
            } else {
                emptyState(report)
            }
        }
        .frame(maxWidth: Self.maxWidth, alignment: .leading)
        .padding(4)
        .animation(motion, value: range)
        .animation(motion, value: grouping)
        .animation(motion, value: metric)
    }

    // MARK: Header

    private var header: some View {
        // The window's narrowest size leaves no room for the title beside the controls.
        ViewThatFits(in: .horizontal) {
            HStack(alignment: .center, spacing: 16) {
                title
                Spacer(minLength: 16)
                controls
            }
            VStack(alignment: .leading, spacing: 12) {
                title
                HStack(spacing: 12) { controls }
            }
        }
    }

    private var title: some View {
        Text(language.t("fleet.usage.title"))
            .font(.title2.weight(.semibold))
            .lineLimit(1)
            .fixedSize()
    }

    @ViewBuilder
    private var controls: some View {
            Picker(selection: $range) {
                ForEach(FleetUsageRange.allCases) { option in
                    Text(language.t("fleet.usage.range.\(option.rawValue)")).tag(option)
                }
            } label: { EmptyView() }
                .pickerStyle(.segmented)
                .labelsHidden()
                .fixedSize()
            Picker(selection: $grouping) {
                ForEach(FleetUsageGrouping.allCases) { option in
                    Text(language.t("fleet.usage.grouping.\(option.rawValue)")).tag(option)
                }
            } label: { EmptyView() }
                .pickerStyle(.segmented)
                .labelsHidden()
                .fixedSize()
            Menu {
                Picker(selection: $metric) {
                    ForEach(FleetUsageMetric.allCases) { option in
                        Text(language.t("fleet.usage.metric.\(option.rawValue)")).tag(option)
                    }
                } label: { EmptyView() }
                    .pickerStyle(.inline)
                    .labelsHidden()
            } label: {
                Text(language.t("fleet.usage.metric.\(metric.rawValue)"))
            }
            .menuStyle(.button)
            .buttonStyle(.borderless)
            .fixedSize()
    }

    // MARK: Hero

    private func hero(_ report: FleetUsageReport) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            ViewThatFits(in: .horizontal) {
                HStack(alignment: .top, spacing: 32) {
                    headline(report)
                    Spacer(minLength: 32)
                    composition(report)
                        .padding(.top, 40)
                }
                VStack(alignment: .leading, spacing: 16) {
                    headline(report)
                    composition(report)
                }
            }
            notes(report)
        }
    }

    private func headline(_ report: FleetUsageReport) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(heroLabel(report))
                .font(.subheadline.weight(.medium))
                .foregroundStyle(.secondary)
            heroNumber(report.total)
            if let change = changeLine(report) {
                Text(change)
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
        }
        .fixedSize()
    }

    /// The figure at full size and its unit, 亿 or B, at half: a CJK unit at 54pt
    /// outweighs the digits it belongs to. `isNumber` won't do, as 亿 is one.
    private func heroNumber(_ total: Int64) -> some View {
        let text = UsageFormat.tokens(total, language)
        let digits = text.lastIndex(where: { $0.unicodeScalars.first?.properties.generalCategory == .decimalNumber }).map { text.index(after: $0) } ?? text.endIndex
        return (Text(verbatim: String(text[..<digits]))
                    .font(.system(size: 54, weight: .semibold))
                + Text(verbatim: String(text[digits...]))
                    .font(.system(size: 27, weight: .medium))
                    .foregroundColor(.secondary))
            .tracking(-1.2)
            .lineLimit(1)
            .contentTransition(.numericText(value: Double(total)))
    }

    private func heroLabel(_ report: FleetUsageReport) -> String {
        let period = language.t("fleet.usage.period.\(report.range.rawValue)")
        guard report.metric == .withoutCacheRead else { return period }
        return period + " · " + language.t("fleet.usage.metric.withoutCacheRead")
    }

    /// `↑ 12% vs previous 7 days`, in neutral ink: more tokens is neither good nor bad.
    private func changeLine(_ report: FleetUsageReport) -> String? {
        guard let change = report.change else { return nil }
        let arrow = abs(change) < 0.005 ? "" : (change > 0 ? "↑ " : "↓ ")
        let key = report.range == .today ? "fleet.usage.vsYesterday" : "fleet.usage.vsPreviousWeek"
        return language.t(key, arrow + UsageFormat.percent(abs(change), language))
    }

    private struct Part: Identifiable {
        let id: String
        let value: Int64
        let counted: Bool
    }

    private func parts(_ report: FleetUsageReport) -> [Part] {
        [Part(id: "input", value: report.parts.input, counted: true),
         Part(id: "output", value: report.parts.output, counted: true),
         Part(id: "cacheRead", value: report.parts.cacheRead, counted: report.metric == .all),
         Part(id: "cacheWrite", value: report.parts.cacheWrite, counted: true)]
    }

    private func composition(_ report: FleetUsageReport) -> some View {
        Grid(alignment: .leading, horizontalSpacing: 24, verticalSpacing: 4) {
            GridRow {
                ForEach(parts(report)) { part in
                    Text(language.t("fleet.usage.part.\(part.id)"))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            }
            GridRow {
                ForEach(parts(report)) { part in
                    Text(verbatim: UsageFormat.tokens(part.value, language))
                        .font(.callout.monospacedDigit())
                        // Left out of the total by the metric, so shown as such.
                        .foregroundStyle(part.counted ? HierarchicalShapeStyle.primary
                                                      : HierarchicalShapeStyle.tertiary)
                        .lineLimit(1)
                }
            }
        }
        .fixedSize()
        .accessibilityElement(children: .combine)
    }

    // MARK: Notes and empty state

    @ViewBuilder
    private func notes(_ report: FleetUsageReport) -> some View {
        let lines = noteLines(report)
        if !lines.isEmpty {
            VStack(alignment: .leading, spacing: 4) {
                ForEach(lines, id: \.self) { line in
                    Text(line)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .font(.caption)
            .foregroundStyle(.secondary)
        }
    }

    /// What is missing, said as missing rather than shown as zero.
    private func noteLines(_ report: FleetUsageReport) -> [String] {
        var lines: [String] = []
        let progress = UsageFormat.percent(report.scanProgress, language)
        if report.scanning.count == 1 {
            lines.append(language.t("fleet.usage.scanningOne", report.scanning[0], progress))
        } else if report.scanning.count > 1 {
            lines.append(language.t("fleet.usage.scanningMany", report.scanning.count, progress))
        }
        if !report.withoutHistory.isEmpty {
            lines.append(language.t("fleet.usage.noHistory", list(report.withoutHistory)))
        }
        if !report.withoutData.isEmpty {
            lines.append(language.t("fleet.usage.noData", list(report.withoutData)))
        }
        if !report.leftOutOfChange.isEmpty {
            lines.append(language.t("fleet.usage.leftOut", list(report.leftOutOfChange)))
        }
        return lines
    }

    private func list(_ names: [String]) -> String {
        let formatter = ListFormatter()
        formatter.locale = language.locale
        return formatter.string(from: names) ?? names.joined(separator: ", ")
    }

    private func emptyState(_ report: FleetUsageReport) -> some View {
        VStack(alignment: .leading, spacing: 16) {
            Text(language.t("fleet.usage.empty"))
                .font(.body)
                .foregroundStyle(.secondary)
                .frame(maxWidth: 480, alignment: .leading)
                .fixedSize(horizontal: false, vertical: true)
            notes(report)
        }
        .padding(.top, 16)
    }
}

// MARK: - Colour and words

/// The categorical palette, in a fixed order checked for colour-blind
/// separation. Picked by colour scheme rather than left to adapt, as the dark
/// set is tuned separately.
struct FleetUsagePalette {
    static let light: [UInt32] = [0x2A78D6, 0xEB6834, 0x1BAF7A, 0xEDA100, 0xE87BA4, 0x008300, 0x6250D6, 0xE34948]
    static let dark: [UInt32] = [0x3987E5, 0xD95926, 0x199E70, 0xC98500, 0xD55181, 0x008300, 0x9085E9, 0xE66767]

    let scheme: ColorScheme

    func color(_ color: FleetUsageColor) -> Color {
        switch color {
        case .slot(let slot):
            let colors = scheme == .dark ? Self.dark : Self.light
            return Color(hex: colors[((slot % colors.count) + colors.count) % colors.count])
        case .vendor(let vendor):
            // `ringTint` lifts the one vendor mark that vanishes on a dark surface.
            return scheme == .dark ? vendor.ringTint : vendor.tint
        case .other:
            return Color(hex: scheme == .dark ? 0x6E6E73 : 0xAEAEB2)
        }
    }
}

enum FleetUsageText {
    static func name(_ name: FleetUsageRow.Name, _ language: AppLanguage) -> String {
        switch name {
        case .text(let text): return text
        case .vendor(let vendor): return vendor.title(language)
        case .unknownClaudeAccount: return language.t("fleet.usage.unknownClaude")
        case .other: return language.t("fleet.usage.other")
        }
    }

    static func machines(_ count: Int, _ language: AppLanguage) -> String {
        count == 1 ? language.t("fleet.usage.machineOne") : language.t("fleet.usage.machineCount", count)
    }

    static func models(_ count: Int, _ language: AppLanguage) -> String {
        count == 1 ? language.t("fleet.usage.modelOne") : language.t("fleet.usage.modelCount", count)
    }

    /// The one secondary line under a row's name.
    static func detail(_ row: FleetUsageRow, devices: [FleetDeviceState], _ language: AppLanguage) -> String {
        var parts: [String] = []
        switch row.detail {
        case .machine(let users):
            if !users.isEmpty { parts.append(users.joined(separator: ", ")) }
            parts += presence(row.deviceIDs, devices: devices, language)
        case .device(let user):
            if let user, !user.isEmpty { parts.append(user) }
            parts += presence(row.deviceIDs, devices: devices, language)
        case .claudeAccount(let plan, let count):
            if let plan, !plan.isEmpty { parts.append(plan) }
            parts.append(machines(count, language))
        case .unknownClaudeAccount(let device):
            parts.append(device)
        case .vendorAccount(let count):
            parts.append(machines(count, language))
        case .vendor(let count):
            parts.append(models(count, language))
        case .model:
            break
        }
        return parts.joined(separator: " · ")
    }

    /// `Online · CPU 42%`, or when an offline machine last reported. A live
    /// reading is never shown for a machine that has gone.
    static func presence(_ ids: [String], devices: [FleetDeviceState], _ language: AppLanguage) -> [String] {
        let states = devices.filter { ids.contains($0.id) }
        if let live = states.first(where: \.isLive) {
            var parts = [language.t("fleet.link.live")]
            if let cpu = live.snapshot?.host.cpuPercent {
                parts.append(language.t("fleet.usage.cpu", FleetText.percent(cpu)))
            }
            return parts
        }
        guard let last = states.compactMap({ $0.usage?.reportedAt }).max() else {
            let connecting = states.contains { $0.link == .connecting }
            return [language.t(connecting ? "fleet.link.connecting" : "fleet.link.offline")]
        }
        return [language.t("fleet.usage.updated",
                           last.formatted(.relative(presentation: .named).locale(language.locale)))]
    }

    static func inputOutput(_ usage: FleetUsage, _ language: AppLanguage) -> String {
        language.t("fleet.usage.inputOutput",
                   UsageFormat.tokens(usage.input, language), UsageFormat.tokens(usage.output, language))
    }

    static func dayTitle(_ key: String, _ language: AppLanguage) -> String {
        guard let date = FleetUsageDay.date(key, calendar: .current) else { return key }
        return date.formatted(.dateTime.weekday(.abbreviated).month(.abbreviated).day().locale(language.locale))
    }

    /// What a chart shows, for VoiceOver. The ledger below is its table.
    static func summary(_ report: FleetUsageReport, _ language: AppLanguage) -> String {
        let period = language.t("fleet.usage.period.\(report.range.rawValue)")
        let largest = report.series.first.map {
            name($0.name, language) + " " + UsageFormat.tokens($0.total, language)
        } ?? FleetText.unknown
        return language.t("fleet.usage.chartSummary", period,
                          UsageFormat.tokens(report.total, language), largest)
    }
}

// MARK: - Daily chart

/// Stacked bars by day, for 7 and 30 days.
private struct FleetUsageChart: View {
    let report: FleetUsageReport
    let palette: FleetUsagePalette
    let language: AppLanguage
    @State private var hovered: String?
    /// The plot's height, to turn the gap and the corner from points into tokens.
    @State private var plotHeight: CGFloat = 190

    private static let height: CGFloat = 220
    private static let gap: CGFloat = 2
    private static let radius: CGFloat = 3

    private struct Segment: Identifiable {
        let id: String
        let day: String
        let start: Double
        let end: Double
        let color: Color
        let rounded: Bool
    }

    private var barWidth: CGFloat { report.dayKeys.count > 7 ? 10 : 24 }

    /// A round number a little above the tallest day, so the top label reads cleanly.
    private var top: Double {
        let tallest = report.dayKeys.indices.map { index in
            report.series.reduce(0.0) { $0 + Double($1.values.indices.contains(index) ? $1.values[index] : 0) }
        }.max() ?? 0
        guard tallest > 0 else { return 1 }
        let magnitude = pow(10, floor(log10(tallest)))
        let step = [1, 2, 3, 4, 5, 6, 8, 10].first { $0 * magnitude >= tallest } ?? 10
        return step * magnitude
    }

    /// Stacked by hand, largest series at the bottom, so each segment can stop
    /// short of the next by the gap, and only the top of a bar is rounded.
    private func segments(top: Double) -> [Segment] {
        let perPoint = top / Double(max(1, plotHeight))
        let gap = Double(Self.gap) * perPoint
        let radius = Double(Self.radius) * perPoint
        var segments: [Segment] = []
        for (index, day) in report.dayKeys.enumerated() {
            let values = report.series.map { $0.values.indices.contains(index) ? Double($0.values[index]) : 0 }
            guard let last = values.lastIndex(where: { $0 > 0 }) else { continue }
            var base = 0.0
            for (position, item) in report.series.enumerated() where values[position] > 0 {
                let end = base + values[position]
                let color = palette.color(item.color)
                if position == last {
                    segments.append(Segment(id: "\(day)/\(item.id)", day: day, start: base, end: end,
                                            color: color, rounded: true))
                    // Squares off the lower corners the rounding took away.
                    if end - radius > base {
                        segments.append(Segment(id: "\(day)/\(item.id)/base", day: day, start: base,
                                                end: end - radius, color: color, rounded: false))
                    }
                } else {
                    segments.append(Segment(id: "\(day)/\(item.id)", day: day, start: base,
                                            end: max(base, end - gap), color: color, rounded: false))
                }
                base = end
            }
        }
        return segments
    }

    /// Every day for a week; for a month, every seventh counting back from today,
    /// leaving today out as its label would run into the y axis.
    private var labelledDays: [String] {
        let keys = report.dayKeys
        guard keys.count > 7 else { return keys }
        return keys.indices.filter { $0 < keys.count - 1 && (keys.count - 1 - $0) % 7 == 0 }.map { keys[$0] }
    }

    private func axisLabel(_ key: String) -> String {
        guard let date = FleetUsageDay.date(key, calendar: .current) else { return key }
        if report.dayKeys.count > 7 { return UsageFormat.day(date, language) }
        return date.formatted(.dateTime.weekday(.abbreviated).locale(language.locale))
    }

    var body: some View {
        let top = self.top
        Chart {
            ForEach(segments(top: top)) { segment in
                BarMark(x: .value("Day", segment.day),
                        yStart: .value("Tokens", segment.start),
                        yEnd: .value("Tokens", segment.end),
                        width: .fixed(barWidth))
                    .foregroundStyle(segment.color)
                    .cornerRadius(segment.rounded ? Self.radius : 0)
            }
            // A day left over from the range before isn't on this axis.
            if let hovered, report.dayKeys.contains(hovered) {
                RuleMark(x: .value("Day", hovered))
                    .foregroundStyle(Color.primary.opacity(0.25))
                    .lineStyle(StrokeStyle(lineWidth: 1))
                    .annotation(position: .trailing, alignment: .top, spacing: 8,
                                overflowResolution: .init(x: .fit(to: .chart), y: .fit(to: .chart))) {
                        tooltip(hovered)
                    }
            }
        }
        .chartXScale(domain: report.dayKeys)
        .chartYScale(domain: 0...top)
        .chartXAxis {
            AxisMarks(values: labelledDays) { value in
                AxisValueLabel {
                    if let key = value.as(String.self) {
                        Text(verbatim: axisLabel(key))
                    }
                }
            }
        }
        .chartYAxis {
            AxisMarks(position: .trailing, values: [0, top / 2, top]) { value in
                AxisGridLine(stroke: StrokeStyle(lineWidth: 0.5))
                    .foregroundStyle(Color.primary.opacity(0.08))
                AxisValueLabel {
                    if let tokens = value.as(Double.self) {
                        Text(verbatim: UsageFormat.tokens(Int64(tokens), language))
                    }
                }
            }
        }
        .chartOverlay { proxy in
            GeometryReader { geometry in
                let frame = proxy.plotFrame.map { geometry[$0] } ?? .zero
                // The whole day's band answers, not just the thin bar in it.
                Rectangle()
                    .fill(Color.clear)
                    .contentShape(Rectangle())
                    .onContinuousHover { phase in
                        switch phase {
                        case .active(let location):
                            hovered = proxy.value(atX: location.x - frame.minX, as: String.self)
                        case .ended:
                            hovered = nil
                        }
                    }
                    .onAppear { plotHeight = frame.height }
                    .onChange(of: frame.height) { _, height in plotHeight = height }
            }
        }
        .frame(height: Self.height)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(Text(FleetUsageText.summary(report, language)))
    }

    private func tooltip(_ day: String) -> some View {
        let index = report.dayKeys.firstIndex(of: day) ?? 0
        let shown = report.series.filter { $0.values.indices.contains(index) && $0.values[index] > 0 }
        let total = report.series.reduce(Int64(0)) {
            $0 + ($1.values.indices.contains(index) ? $1.values[index] : 0)
        }
        return VStack(alignment: .leading, spacing: 4) {
            Text(verbatim: FleetUsageText.dayTitle(day, language))
                .foregroundStyle(.secondary)
            HStack(spacing: 16) {
                Text(language.t("fleet.usage.total"))
                Spacer(minLength: 0)
                Text(verbatim: UsageFormat.tokens(total, language))
                    .monospacedDigit()
            }
            .font(.caption.weight(.semibold))
            ForEach(shown) { item in
                HStack(spacing: 6) {
                    RoundedRectangle(cornerRadius: 2)
                        .fill(palette.color(item.color))
                        .frame(width: 8, height: 8)
                    Text(verbatim: FleetUsageText.name(item.name, language))
                        .lineLimit(1)
                    Spacer(minLength: 16)
                    Text(verbatim: UsageFormat.tokens(item.values[index], language))
                        .monospacedDigit()
                }
            }
        }
        .font(.caption)
        .padding(8)
        .frame(minWidth: 160, maxWidth: 240)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 6))
        .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(Color.primary.opacity(0.1), lineWidth: 0.5))
    }
}

/// What a colour means, in text ink beside its mark.
private struct FleetUsageLegend: View {
    let series: [FleetUsageSeries]
    let palette: FleetUsagePalette
    let language: AppLanguage

    var body: some View {
        FleetUsageFlow(spacing: 16, lineSpacing: 8) {
            ForEach(series) { item in
                HStack(spacing: 6) {
                    RoundedRectangle(cornerRadius: 2)
                        .fill(palette.color(item.color))
                        .frame(width: 8, height: 8)
                    Text(verbatim: FleetUsageText.name(item.name, language))
                        .font(.caption)
                        .lineLimit(1)
                }
            }
        }
        .accessibilityHidden(true)
    }
}

/// Lays its children out in lines, wrapping where the width runs out.
private struct FleetUsageFlow: Layout {
    var spacing: CGFloat
    var lineSpacing: CGFloat

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        arrange(width: proposal.width ?? .infinity, subviews: subviews).size
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        let points = arrange(width: bounds.width, subviews: subviews).points
        for (index, subview) in subviews.enumerated() {
            subview.place(at: CGPoint(x: bounds.minX + points[index].x, y: bounds.minY + points[index].y),
                          proposal: .unspecified)
        }
    }

    private func arrange(width: CGFloat, subviews: Subviews) -> (points: [CGPoint], size: CGSize) {
        var points: [CGPoint] = []
        var x: CGFloat = 0, y: CGFloat = 0, lineHeight: CGFloat = 0, widest: CGFloat = 0
        for subview in subviews {
            let size = subview.sizeThatFits(.unspecified)
            if x > 0, x + size.width > width {
                x = 0
                y += lineHeight + lineSpacing
                lineHeight = 0
            }
            points.append(CGPoint(x: x, y: y))
            widest = max(widest, x + size.width)
            x += size.width + spacing
            lineHeight = max(lineHeight, size.height)
        }
        return (points, CGSize(width: widest, height: y + lineHeight))
    }
}

// MARK: - Today

/// One day has no series to draw, so: one bar across the page, labelled in place.
private struct FleetUsageTodayBar: View {
    let report: FleetUsageReport
    let palette: FleetUsagePalette
    let language: AppLanguage

    private static let barHeight: CGFloat = 12
    private static let gap: CGFloat = 2
    /// Narrower segments go unlabelled here; the ledger below still has them.
    private static let labelWidth: CGFloat = 72

    private static func widths(_ totals: [Int64], in width: CGFloat) -> [CGFloat] {
        let sum = Double(max(1, totals.reduce(0, +)))
        let available = max(0, width - gap * CGFloat(max(0, totals.count - 1)))
        return totals.map { max(1, available * CGFloat(Double($0) / sum)) }
    }

    var body: some View {
        let series = report.series.filter { $0.total > 0 }
        GeometryReader { geometry in
            let widths = Self.widths(series.map(\.total), in: geometry.size.width)
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: Self.gap) {
                    ForEach(series.indices, id: \.self) { index in
                        Rectangle()
                            .fill(palette.color(series[index].color))
                            .frame(width: widths[index], height: Self.barHeight)
                    }
                }
                .clipShape(RoundedRectangle(cornerRadius: 3))
                HStack(alignment: .top, spacing: Self.gap) {
                    ForEach(series.indices, id: \.self) { index in
                        VStack(alignment: .leading, spacing: 2) {
                            if widths[index] >= Self.labelWidth {
                                Text(verbatim: FleetUsageText.name(series[index].name, language))
                                    .font(.caption)
                                Text(verbatim: UsageFormat.tokens(series[index].total, language) + " · "
                                     + UsageFormat.share(Double(series[index].total)
                                                         / Double(max(1, report.total)), language))
                                    .font(.caption.monospacedDigit())
                                    .foregroundStyle(.secondary)
                            }
                        }
                        .lineLimit(1)
                        .frame(width: widths[index], alignment: .leading)
                    }
                }
            }
        }
        .frame(height: 56)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(Text(FleetUsageText.summary(report, language)))
    }
}

// MARK: - Ledger

/// The table under the chart: every row, its trend, share and tokens.
private struct FleetUsageLedger: View {
    let report: FleetUsageReport
    let devices: [FleetDeviceState]
    let palette: FleetUsagePalette
    let language: AppLanguage
    let onSelect: (String) -> Void
    @State private var expanded: Set<String> = []
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    static let trendWidth: CGFloat = 88
    static let shareWidth: CGFloat = 56
    static let tokensWidth: CGFloat = 80
    /// Disclosure, mark and the spacing after each.
    static let nameInset: CGFloat = 12 + 12 + 8 + 12
    static let childIndent: CGFloat = 24

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            columns
            Divider()
            ForEach(report.rows) { row in
                line(row, depth: 0)
                if expanded.contains(row.id) {
                    ForEach(row.children) { child in
                        Divider()
                            .padding(.leading, Self.nameInset + Self.childIndent)
                        line(child, depth: 1)
                    }
                    .transition(.opacity)
                }
                Divider()
            }
        }
    }

    private var columns: some View {
        HStack(spacing: 12) {
            Text(language.t("fleet.usage.grouping.\(report.grouping.rawValue)"))
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.leading, Self.nameInset)
            Text(language.t("fleet.usage.column.trend"))
                .frame(width: Self.trendWidth, alignment: .leading)
            Text(language.t("fleet.usage.column.share"))
                .frame(width: Self.shareWidth, alignment: .trailing)
            Text(language.t("fleet.usage.column.tokens"))
                .frame(width: Self.tokensWidth, alignment: .trailing)
        }
        .font(.subheadline.weight(.medium))
        .foregroundStyle(.secondary)
        .lineLimit(1)
        .padding(.horizontal, 8)
        .padding(.bottom, 8)
    }

    private func line(_ row: FleetUsageRow, depth: Int) -> some View {
        FleetUsageLedgerRow(
            row: row, depth: depth,
            detail: FleetUsageText.detail(row, devices: devices, language),
            hoverDetail: report.grouping == .models ? FleetUsageText.inputOutput(row.usage, language) : nil,
            palette: palette, language: language,
            isExpanded: expanded.contains(row.id),
            onToggle: row.children.isEmpty ? nil : { toggle(row.id) },
            onSelect: selection(for: row))
    }

    /// Machines and devices open in the sidebar, as Overview cards do.
    private func selection(for row: FleetUsageRow) -> (() -> Void)? {
        switch row.detail {
        case .machine, .device:
            guard let id = row.deviceIDs.first else { return nil }
            return { onSelect(id) }
        default:
            return nil
        }
    }

    private func toggle(_ id: String) {
        withAnimation(reduceMotion ? nil : .smooth(duration: 0.3)) {
            if expanded.contains(id) {
                expanded.remove(id)
            } else {
                expanded.insert(id)
            }
        }
    }
}

private struct FleetUsageLedgerRow: View {
    let row: FleetUsageRow
    let depth: Int
    let detail: String
    /// Shown instead of `detail` while the pointer is over the row.
    let hoverDetail: String?
    let palette: FleetUsagePalette
    let language: AppLanguage
    let isExpanded: Bool
    let onToggle: (() -> Void)?
    let onSelect: (() -> Void)?
    @State private var hovering = false

    var body: some View {
        HStack(spacing: 12) {
            disclosure
            RoundedRectangle(cornerRadius: 2)
                .fill(depth == 0 ? palette.color(row.color) : Color.clear)
                .frame(width: 8, height: 8)
            VStack(alignment: .leading, spacing: 2) {
                Text(verbatim: FleetUsageText.name(row.name, language))
                    .lineLimit(1)
                Text(verbatim: shownDetail.isEmpty ? " " : shownDetail)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.leading, CGFloat(depth) * FleetUsageLedger.childIndent)
            FleetUsageSparkline(values: row.trend)
                .frame(width: FleetUsageLedger.trendWidth, height: 20)
            Text(verbatim: UsageFormat.share(row.share, language))
                .font(.callout.monospacedDigit())
                .foregroundStyle(.secondary)
                .frame(width: FleetUsageLedger.shareWidth, alignment: .trailing)
            Text(verbatim: UsageFormat.tokens(row.tokens, language))
                .font(.body.monospacedDigit())
                .frame(width: FleetUsageLedger.tokensWidth, alignment: .trailing)
        }
        .padding(.vertical, 8)
        .padding(.horizontal, 8)
        .background(RoundedRectangle(cornerRadius: 6)
            .fill(hovering && onSelect != nil ? Color.primary.opacity(0.04) : Color.clear))
        .contentShape(Rectangle())
        .onHover { hovering = $0 }
        .onTapGesture { onSelect?() }
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(onSelect == nil ? [] : .isButton)
    }

    private var shownDetail: String {
        // A row with nothing else to say keeps it on show.
        if let hoverDetail, hovering || detail.isEmpty { return hoverDetail }
        return detail
    }

    @ViewBuilder
    private var disclosure: some View {
        if let onToggle {
            Button(action: onToggle) {
                Image(systemName: "chevron.right")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
                    .rotationEffect(.degrees(isExpanded ? 90 : 0))
                    .frame(width: 12, height: 12)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
        } else {
            Color.clear.frame(width: 12, height: 12)
        }
    }
}

/// Thirty days as one quiet line, in muted ink: it shows shape, not identity.
private struct FleetUsageSparkline: View {
    let values: [Int64]

    var body: some View {
        let top = Double(max(1, values.max() ?? 0))
        Chart {
            ForEach(values.indices, id: \.self) { index in
                LineMark(x: .value("Day", index), y: .value("Tokens", Double(values[index])))
                    .interpolationMethod(.monotone)
                    .lineStyle(StrokeStyle(lineWidth: 1.5, lineCap: .round, lineJoin: .round))
                    .foregroundStyle(Color.secondary)
            }
        }
        .chartXAxis(.hidden)
        .chartYAxis(.hidden)
        .chartLegend(.hidden)
        .chartXScale(domain: 0...max(1, values.count - 1))
        .chartYScale(domain: 0...top)
        .accessibilityHidden(true)
    }
}

// MARK: - Fixture

#if DEBUG
extension FleetUsageView {
    /// Four devices with a month of plausible use, for rendering the page to
    /// images: one machine with two OS accounts, two other machines, one of
    /// them offline with a cached history, and one Claude account on two machines.
    @MainActor
    static func fixtureStore(now: Date = Date(), calendar: Calendar = .current) -> FleetStore {
        var random = FixtureRandom(seed: 0x70C0)
        let personal = FleetClaudeAccount(email: "jacky@example.com", plan: "Claude Max", organization: nil)
        let team = FleetClaudeAccount(email: "jacky@acme.dev", plan: "Claude Team", organization: "Acme")

        func host(_ name: String, _ system: String, _ machine: String, cpus: Int,
                  cpu: Double, user: String) -> FleetSnapshot.Host {
            FleetSnapshot.Host(hostname: name, system: system, machine: machine, cpus: cpus,
                               load: cpu / 100 * Double(cpus), cpuPercent: cpu, temperature: 48 + cpu / 3,
                               user: user)
        }

        func usage(_ total: Double, model: String) -> FleetUsage {
            // Claude Code leans on its cache far more than the other vendors' tools.
            let claude = Vendor.inferred(fromModelID: model) == .anthropic
            return FleetUsage(input: Int64(total * (claude ? 0.04 : 0.2)),
                              output: Int64(total * (claude ? 0.03 : 0.07)),
                              cacheRead: Int64(total * (claude ? 0.84 : 0.73)),
                              cacheWrite: Int64(total * (claude ? 0.09 : 0)))
        }

        /// A month of one device's use, ending `ago` days back: its history and that day.
        func month(_ daily: [String: Double], endingDaysAgo ago: Int,
                   weekendFactor: Double) -> (FleetUsageHistory, [String: FleetUsage]) {
            var days: [String: [String: FleetUsage]] = [:]
            let keys = FleetUsageDay.keys(29, endingDaysAgo: ago + 1, now: now, calendar: calendar)
            let models = daily.sorted { $0.key < $1.key }
            for (offset, key) in keys.enumerated() {
                let weekday = FleetUsageDay.date(key, calendar: calendar).map { calendar.component(.weekday, from: $0) }
                let weekend = weekday == 1 || weekday == 7
                // Busier lately, quieter at weekends, and now and then a day off.
                let trend = 0.7 + 0.5 * Double(offset) / Double(keys.count)
                for (model, base) in models {
                    guard random.next() > 0.08 else { continue }
                    let scale = base * trend * (weekend ? weekendFactor : 1) * (0.55 + 0.9 * random.next())
                    days[key, default: [:]][model] = usage(scale, model: model)
                }
            }
            var today: [String: FleetUsage] = [:]
            for (model, base) in models {
                today[model] = usage(base * (0.35 + 0.4 * random.next()), model: model)
            }
            return (FleetUsageHistory(complete: true, progress: 1, days: days), today)
        }

        func live(_ device: FleetDevice, _ host: FleetSnapshot.Host, account: FleetClaudeAccount?,
                  daily: [String: Double], weekendFactor: Double = 0.4) -> FleetDeviceState {
            let (history, today) = month(daily, endingDaysAgo: 0, weekendFactor: weekendFactor)
            let snapshot = FleetSnapshot(agentVersion: 5, host: host, sessions: [], todayByModel: today,
                                         workers: [], claudeAccount: account, usageHistory: history)
            return FleetDeviceState(device: device, link: .live(via: device.ssh.first),
                                    snapshot: snapshot, updatedAt: now)
        }

        let studio = live(FleetDevice(id: "studio", label: "Mac Studio"),
                          host("studio", "Darwin", "arm64", cpus: 24, cpu: 18, user: "jacky"),
                          account: personal,
                          daily: ["claude-opus-5-5": 62_000_000, "claude-sonnet-5-5": 24_000_000])
        let ci = live(FleetDevice(id: "studio-ci", label: "Mac Studio · ci", ssh: ["studio"], sudoUser: "ci"),
                      host("studio", "Darwin", "arm64", cpus: 24, cpu: 18, user: "ci"),
                      account: nil,
                      daily: ["MiniMax-M2.7": 31_000_000, "glm-5.2": 9_000_000], weekendFactor: 0.9)
        let gpu = live(FleetDevice(id: "gpu", label: "gpu-box", ssh: ["gpu-box"]),
                       host("gpu-box", "Linux", "x86_64", cpus: 64, cpu: 42, user: "jacky"),
                       account: personal,
                       daily: ["claude-sonnet-5-5": 33_000_000, "MiniMax-M2.7-highspeed": 44_000_000,
                               "glm-5.2": 17_000_000], weekendFactor: 0.7)

        let lastSeen = calendar.date(byAdding: .day, value: -2, to: now) ?? now
        let (history, today) = month(["claude-sonnet-5-5": 14_000_000, "claude-haiku-5-5": 3_500_000],
                                     endingDaysAgo: 2, weekendFactor: 0.1)
        let laptop = FleetDeviceState(
            device: FleetDevice(id: "air", label: "MacBook Air", ssh: ["air"]),
            link: .offline(reason: "ssh: connect to host air port 22: Operation timed out"),
            snapshot: nil, updatedAt: lastSeen,
            usage: FleetUsageRecord(hostname: "air", user: "jacky", claudeAccount: team,
                                    todayByModel: today, reportedAt: lastSeen, history: history))

        return FleetStore(fixture: [studio, ci, gpu, laptop])
    }
}

/// The same month every time, so renders can be compared.
private struct FixtureRandom {
    var state: UInt64

    init(seed: UInt64) { state = seed }

    mutating func next() -> Double {
        state = state &* 6_364_136_223_846_793_005 &+ 1_442_695_040_888_963_407
        return Double(state >> 11) / Double(UInt64(1) << 53)
    }
}
#endif
