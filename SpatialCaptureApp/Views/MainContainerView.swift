import SwiftUI

struct MainContainerView: View {
    @EnvironmentObject private var manager: SpatialCaptureManager

    var body: some View {
        ZStack(alignment: .bottom) {
            content
                .ignoresSafeArea()

            controlsBar
                .padding()
        }
        .task {
            if manager.permissionStatus == .unknown {
                await manager.requestPermissionsIfNeeded()
            }
        }
        .alert("Scan Error", isPresented: Binding(
            get: { manager.activeError != nil },
            set: { if !$0 { manager.activeError = nil } }
        )) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(manager.activeError ?? "Unknown error")
        }
    }

    @ViewBuilder
    private var content: some View {
        switch manager.appState {
        case .idle, .scanning:
            ScanningView()
        case .results:
            ResultsView()
        }
    }

    private var controlsBar: some View {
        HStack(spacing: 12) {
            Button("Start Scan") {
                manager.startScan()
            }
            .buttonStyle(.borderedProminent)
            .disabled(manager.appState == .scanning || manager.permissionStatus != .granted)

            Button("Stop Scan") {
                manager.stopScan()
            }
            .buttonStyle(.bordered)
            .disabled(manager.appState != .scanning)

            Button("Reset") {
                manager.resetAll()
            }
            .buttonStyle(.bordered)
        }
        .padding(10)
        .background(.ultraThinMaterial)
        .clipShape(Capsule())
    }
}
