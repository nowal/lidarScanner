import SwiftUI

struct ResultsView: View {
    @EnvironmentObject private var manager: SpatialCaptureManager

    var body: some View {
        TabView {
            MeshResultView(meshSnapshots: manager.meshSnapshots)
                .tabItem {
                    Label("3D Scan", systemImage: "cube.transparent")
                }

            FloorplanCanvasView(capturedRoom: manager.capturedRoom)
                .tabItem {
                    Label("2D Floorplan", systemImage: "square.grid.2x2")
                }
        }
    }
}
