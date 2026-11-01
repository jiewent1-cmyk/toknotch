import SwiftUI

/// The settings control, below the notch.
///
/// At rest it is a single arc — a segment of a circle's edge, tucked into the
/// corner the notch's bottom flare makes. On hover that same circle fills in and
/// takes a gear. The two states are the same circle, which is what makes the
/// change read as one object waking up rather than as one thing being swapped
/// for another.
///
/// It is a bare arc at rest because the notch is meant to be glanceable: a
/// permanently visible gear is a second thing competing with the readings, and
/// the readings are the point. An arc says "there is something here" without
/// asserting anything.
struct SettingsOrb: View {
    let isHovered: Bool
    var edge: NotchEdge = .right
    /// True when the arc traces the bar's own rounded corner from outside
    /// rather than a flare from inside — a flush bar has no flare to tuck into.
    var convex: Bool = false
    /// The circle the resting arc follows.
    var arcRadius: CGFloat = NotchLayout.orbArcRadius
    /// How far the arc sits from the button. Zero inside a flare's pocket,
    /// where the two are the same object; back onto the corner when the button
    /// has had to move clear of the bar.
    var arcOffset: CGSize = .zero
    /// The resting arc's scale as the notch folds, about its own circle so it
    /// moves into the black rather than toward the button.
    var mergeScale: CGFloat = 1
    /// What the filled button shows. The same orb serves as the remote-sync
    /// handle at the other end of the notch.
    var symbol: String = "gearshape"
    /// Drawn for the near end of the notch rather than the far one: the arc is
    /// reflected along the stack, so it tucks into that end's flare instead.
    var mirrored: Bool = false
    /// Work in flight: stays filled with the glyph turning, hovered or not.
    var isBusy: Bool = false

    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var isShown: Bool { isHovered || isBusy }

    /// One turn every 1.2s.
    private static func turn(at date: Date) -> Double {
        date.timeIntervalSinceReferenceDate.truncatingRemainder(dividingBy: 1.2) / 1.2 * 360
    }

    /// Reflects along the stack only — across it the two ends are the same.
    private var mirror: CGSize {
        guard mirrored else { return CGSize(width: 1, height: 1) }
        return edge == .left || edge == .right
            ? CGSize(width: 1, height: -1)
            : CGSize(width: -1, height: 1)
    }

    /// Which quarter of the circle the resting arc occupies.
    ///
    /// The arc has to parallel the flare at the far end of the notch, so it
    /// faces two ways at once: **back along the stack**, toward the notch it
    /// hangs off, and **outward**, toward the bezel it is about to merge into.
    /// On the right edge that is twelve o'clock round to three, which is the
    /// arc this was drawn as before there was any choice of edge. Turn the
    /// notch and the same two directions pick a different quadrant.
    ///
    /// SwiftUI's `Circle` trim starts at three o'clock and runs clockwise, with
    /// y growing downward.
    /// Hugging a corner from outside is the same relationship as hugging a
    /// flare from inside, turned through half a circle.
    static func restingTrim(for edge: NotchEdge, convex: Bool) -> ClosedRange<CGFloat> {
        let concave = restingTrim(for: edge)
        guard convex else { return concave }
        let turned = (concave.lowerBound + 0.5).truncatingRemainder(dividingBy: 1)
        return turned...(turned + 0.25)
    }

    static func restingTrim(for edge: NotchEdge) -> ClosedRange<CGFloat> {
        switch edge {
        case .right:  return 0.75...1.0      // up, round to the right
        case .left:   return 0.5...0.75      // left, round to up
        case .top:    return 0.5...0.75      // left, round to up
        case .bottom: return 0.25...0.5      // down, round to the left
        }
    }

    private var restingTrim: ClosedRange<CGFloat> { Self.restingTrim(for: edge, convex: convex) }



    var body: some View {
        ZStack {
            // The resting arc, on a circle one gap inside the flare's own.
            Circle()
                .trim(from: restingTrim.lowerBound, to: restingTrim.upperBound)
                .stroke(
                    Palette.notch,
                    style: StrokeStyle(lineWidth: NotchLayout.orbStroke, lineCap: .round)
                )
                .frame(width: arcRadius * 2, height: arcRadius * 2)
                .opacity(isShown ? 0 : 1)
                .scaleEffect(isShown ? 0.86 : 1)
                .scaleEffect(mergeScale)
                .offset(arcOffset)
                // After the offset, so the arc's distance from the button is
                // reflected along with it.
                .scaleEffect(x: mirror.width, y: mirror.height)

            Circle()
                .fill(Palette.notch)
                .frame(width: NotchLayout.orbDiameter, height: NotchLayout.orbDiameter)
                .opacity(isShown ? 1 : 0)
                .scaleEffect(isShown ? 1 : 1.1)

            // Turned from the clock rather than by a repeating animation: the
            // angle is simply zero once the work is done, where a repeating
            // animation keeps going after its value has been put back.
            TimelineView(.animation(paused: !isBusy || reduceMotion)) { context in
                Image(systemName: symbol)
                    .font(.system(size: NotchLayout.orbGlyph, weight: .regular))
                    .foregroundStyle(Palette.textPrimary)
                    .rotationEffect(.degrees(isBusy && !reduceMotion ? Self.turn(at: context.date) : 0))
            }
            .opacity(isShown ? 1 : 0)
            .scaleEffect(isShown ? 1 : 0.5)
            .rotationEffect(.degrees(isShown ? 0 : -60))
        }
        // Sized to the larger of the two states, and never clipped: the arc
        // may sit well outside this frame when it has stayed back on the
        // corner the button hangs from.
        .frame(width: arcRadius * 2 + NotchLayout.orbStroke,
               height: arcRadius * 2 + NotchLayout.orbStroke)
        .animation(
            NotchMotion.respectingReduceMotion(
                .spring(response: 0.36, dampingFraction: 0.7), reduceMotion
            ),
            value: isShown
        )
    }
}
