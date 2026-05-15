import ARKit
import RealityKit
import SwiftUI

struct ScanningView: UIViewRepresentable {
    @EnvironmentObject private var manager: SpatialCaptureManager

    func makeUIView(context: Context) -> ARView {
        let arView = ARView(frame: .zero)
        arView.environment.sceneUnderstanding.options.insert(.occlusion)
        arView.environment.sceneUnderstanding.options.insert(.physics)
        arView.debugOptions = [.showSceneUnderstanding, .showAnchorOrigins]
        arView.automaticallyConfigureSession = false
        arView.session = manager.arSession
        return arView
    }

    func updateUIView(_ uiView: ARView, context: Context) {
        if uiView.session !== manager.arSession {
            uiView.session = manager.arSession
        }
    }
}
