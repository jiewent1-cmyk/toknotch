import AppKit
import SwiftUI

/// Hosts the Fleet view. The connections run while the window is open and stop
/// when it closes, so nothing polls the other machines unless someone is looking.
@MainActor
final class FleetWindowController: NSObject, NSWindowDelegate {
    private var window: NSWindow?
    private let store: FleetStore
    private let preferences: Preferences

    init(store: FleetStore, preferences: Preferences) {
        self.store = store
        self.preferences = preferences
    }

    func show() {
        if window?.isVisible != true { store.start() }
        if let window {
            surface(window)
            return
        }
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: FleetView.minWidth + 120,
                                height: FleetView.minHeight + 120),
            styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        window.title = "Fleet"
        window.titlebarAppearsTransparent = true
        window.contentView = NSHostingView(rootView: FleetView(store: store, preferences: preferences))
        window.center()
        window.setFrameAutosaveName("TokNotchFleet")
        window.isReleasedWhenClosed = false
        window.delegate = self
        self.window = window
        surface(window)
    }

    /// See `SettingsWindowController.surface`: an accessory app has to insist.
    private func surface(_ window: NSWindow) {
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
        window.orderFrontRegardless()
    }

    func windowWillClose(_ notification: Notification) {
        store.stop()
    }
}
