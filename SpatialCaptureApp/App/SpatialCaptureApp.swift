import SwiftUI

@main
struct SpatialCaptureApp: App {
    @StateObject private var captureManager = SpatialCaptureManager()

    var body: some Scene {
        WindowGroup {
            MainContainerView()
                .environmentObject(captureManager)
                .onAppear {
                    #if canImport(React)
                    SpatialCaptureBridge.manager = captureManager
                    #endif
                }
        }
    }
}
