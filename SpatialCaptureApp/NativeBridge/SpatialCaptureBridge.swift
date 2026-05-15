import Foundation

#if canImport(React)
import React

@objc(SpatialCaptureBridge)
final class SpatialCaptureBridge: RCTEventEmitter {
    static weak var manager: SpatialCaptureManager?

    @objc override static func requiresMainQueueSetup() -> Bool { true }

    @objc override func supportedEvents() -> [String]! {
        ["spatialCaptureState"]
    }

    @objc func startScan() {
        Task { @MainActor in
            SpatialCaptureBridge.manager?.startScan()
            emitState()
        }
    }

    @objc func stopScan() {
        Task { @MainActor in
            SpatialCaptureBridge.manager?.stopScan()
            emitState()
        }
    }

    @objc func reset() {
        Task { @MainActor in
            SpatialCaptureBridge.manager?.resetAll()
            emitState()
        }
    }

    @objc func getState(_ resolve: RCTPromiseResolveBlock, reject: RCTPromiseRejectBlock) {
        Task { @MainActor in
            guard let manager = SpatialCaptureBridge.manager else {
                reject("NO_MANAGER", "SpatialCaptureManager unavailable", nil)
                return
            }
            resolve(statePayload(manager: manager))
        }
    }

    private func emitState() {
        guard let manager = SpatialCaptureBridge.manager else { return }
        sendEvent(withName: "spatialCaptureState", body: statePayload(manager: manager))
    }

    private func statePayload(manager: SpatialCaptureManager) -> [String: Any] {
        [
            "appState": String(describing: manager.appState),
            "meshCount": manager.meshSnapshots.count,
            "hasRoom": manager.capturedRoom != nil
        ]
    }
}
#endif
