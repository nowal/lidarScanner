import ARKit
import AVFoundation
import Combine
import Foundation
import RoomPlan

@MainActor
final class SpatialCaptureManager: NSObject, ObservableObject {
    @Published var appState: AppState = .idle
    @Published var permissionStatus: PermissionStatus = .unknown
    @Published var capturedRoom: CapturedRoom?
    @Published var meshSnapshots: [ARMeshSnapshot] = []
    @Published var activeError: String?

    let arSession = ARSession()
    let roomCaptureSession = RoomCaptureSession()

    private var meshAnchorCache: [UUID: ARMeshSnapshot] = [:]
    private var cancellables = Set<AnyCancellable>()

    override init() {
        super.init()
        arSession.delegate = self
        roomCaptureSession.delegate = self
    }

    func requestPermissionsIfNeeded() async {
        let camera = await AVCaptureDevice.requestAccess(for: .video)
        permissionStatus = camera ? .granted : .denied
        if !camera {
            activeError = "Camera permission is required to run LiDAR scanning."
        }
    }

    func startScan() {
        guard permissionStatus == .granted else {
            activeError = "Camera access not granted."
            return
        }

        guard ARWorldTrackingConfiguration.supportsSceneReconstruction(.meshWithClassification) else {
            activeError = "This device does not support LiDAR mesh reconstruction."
            return
        }

        resetTransientState()

        let config = ARWorldTrackingConfiguration()
        config.sceneReconstruction = .meshWithClassification
        config.planeDetection = [.horizontal, .vertical]
        config.environmentTexturing = .automatic
        config.worldAlignment = .gravity

        arSession.run(config, options: [.resetTracking, .removeExistingAnchors])

        var roomConfig = RoomCaptureSession.Configuration()
        roomCaptureSession.run(configuration: roomConfig, arSession: arSession)

        appState = .scanning
    }

    func stopScan() {
        guard appState == .scanning else { return }
        roomCaptureSession.stop()
        arSession.pause()
    }

    func resetAll() {
        roomCaptureSession.stop()
        arSession.pause()
        capturedRoom = nil
        meshSnapshots = []
        activeError = nil
        appState = .idle
        meshAnchorCache.removeAll()
    }

    private func resetTransientState() {
        capturedRoom = nil
        meshSnapshots = []
        activeError = nil
        meshAnchorCache.removeAll()
    }
}

extension SpatialCaptureManager: ARSessionDelegate {
    nonisolated func session(_ session: ARSession, didAdd anchors: [ARAnchor]) {
        cacheMeshAnchors(from: anchors)
    }

    nonisolated func session(_ session: ARSession, didUpdate anchors: [ARAnchor]) {
        cacheMeshAnchors(from: anchors)
    }

    private nonisolated func cacheMeshAnchors(from anchors: [ARAnchor]) {
        let snapshots = anchors.compactMap { anchor -> ARMeshSnapshot? in
            guard let meshAnchor = anchor as? ARMeshAnchor else { return nil }
            return ARMeshSnapshot(anchor: meshAnchor)
        }

        guard !snapshots.isEmpty else { return }

        Task { @MainActor in
            for snapshot in snapshots {
                meshAnchorCache[snapshot.id] = snapshot
            }
        }
    }
}

extension SpatialCaptureManager: RoomCaptureSessionDelegate {
    nonisolated func captureSession(_ session: RoomCaptureSession, didEndWith data: CapturedRoomData, error: (any Error)?) {
        Task {
            do {
                if let error {
                    throw error
                }

                let finalRoom = try await data.finalize()
                await MainActor.run {
                    capturedRoom = finalRoom
                    meshSnapshots = Array(meshAnchorCache.values)
                    appState = .results
                }
            } catch {
                await MainActor.run {
                    activeError = "Unable to finalize room: \(error.localizedDescription)"
                    appState = .idle
                }
            }
        }
    }
}

enum PermissionStatus {
    case unknown
    case granted
    case denied
}
