# SpatialCaptureApp (iOS 17+)

This folder contains a complete SwiftUI app implementation for a unified LiDAR pipeline:
- One shared `ARSession` running mesh reconstruction
- One `RoomCaptureSession` injected with that shared `ARSession`
- Real-time scanning viewport
- Post-scan results tabs: 3D mesh + 2D floorplan

## Files
- `App/SpatialCaptureApp.swift`
- `Core/SpatialCaptureManager.swift`
- `Models/AppState.swift`
- `Models/ARMeshSnapshot.swift`
- `Views/MainContainerView.swift`
- `Views/ScanningView.swift`
- `Views/ResultsView.swift`
- `Views/MeshResultView.swift`
- `Views/FloorplanCanvasView.swift`
- `NativeBridge/SpatialCaptureBridge.swift` (optional React Native bridge)
- `ReactNative/SpatialCaptureScreen.tsx` (optional RN screen)

## Xcode Setup
1. Create a new **iOS App** project in Xcode (SwiftUI, Swift, iOS 17+).
2. Drag these folders into your project target.
3. Add frameworks to the app target:
   - `ARKit.framework`
   - `RealityKit.framework`
   - `RoomPlan.framework`
   - `SceneKit.framework`
4. Add Info.plist keys from `InfoPlist.snippet.xml`.
5. Build and run on a **real LiDAR device** (iPhone Pro/iPad Pro).

## React Native Usage (Optional)
- Keep scanning/rendering native for performance.
- Use `NativeBridge/SpatialCaptureBridge.swift` + `ReactNative/SpatialCaptureScreen.tsx` to drive scan controls and state from RN.
- Bridge compiles only when `React` is available (`#if canImport(React)`).

## Notes
- Data persistence is intentionally omitted.
- `AppState` values are `.idle`, `.scanning`, `.results`.
- Final `CapturedRoom` is produced asynchronously with `try await data.finalize()`.
