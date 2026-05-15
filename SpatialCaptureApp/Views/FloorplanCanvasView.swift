import RoomPlan
import SwiftUI
import simd

struct FloorplanCanvasView: View {
    let capturedRoom: CapturedRoom?

    var body: some View {
        GeometryReader { geo in
            if let capturedRoom {
                Canvas { context, size in
                    let plan = FloorplanExtractor.extract(from: capturedRoom, canvasSize: size)

                    for line in plan.walls {
                        var path = Path()
                        path.move(to: line.start)
                        path.addLine(to: line.end)
                        context.stroke(path, with: .color(.primary), lineWidth: 3)
                    }

                    for line in plan.doors {
                        var path = Path()
                        path.move(to: line.start)
                        path.addLine(to: line.end)
                        context.stroke(path, with: .color(.green), lineWidth: 2)
                    }

                    for line in plan.windows {
                        var path = Path()
                        path.move(to: line.start)
                        path.addLine(to: line.end)
                        context.stroke(path, with: .color(.blue), lineWidth: 2)
                    }
                }
                .background(Color(.systemBackground))
            } else {
                ContentUnavailableView("No Floorplan", systemImage: "square.slash", description: Text("Run a scan to generate RoomPlan output."))
                    .frame(width: geo.size.width, height: geo.size.height)
            }
        }
    }
}

private enum FloorplanExtractor {
    struct Line {
        let start: CGPoint
        let end: CGPoint
    }

    struct Plan {
        let walls: [Line]
        let doors: [Line]
        let windows: [Line]
    }

    static func extract(from room: CapturedRoom, canvasSize: CGSize) -> Plan {
        let wallSegments = room.walls.map { wall in segment(from: wall.transform, length: wall.dimensions.x) }
        let doorSegments = room.doors.map { door in segment(from: door.transform, length: door.dimensions.x) }
        let windowSegments = room.windows.map { window in segment(from: window.transform, length: window.dimensions.x) }

        let all = wallSegments + doorSegments + windowSegments
        guard !all.isEmpty else { return Plan(walls: [], doors: [], windows: []) }

        let minX = all.map { min($0.0.x, $0.1.x) }.min() ?? 0
        let maxX = all.map { max($0.0.x, $0.1.x) }.max() ?? 1
        let minZ = all.map { min($0.0.y, $0.1.y) }.min() ?? 0
        let maxZ = all.map { max($0.0.y, $0.1.y) }.max() ?? 1

        let dx = max(maxX - minX, 0.001)
        let dz = max(maxZ - minZ, 0.001)

        let padding: CGFloat = 24
        let scaleX = (canvasSize.width - (padding * 2)) / CGFloat(dx)
        let scaleY = (canvasSize.height - (padding * 2)) / CGFloat(dz)
        let scale = min(scaleX, scaleY)

        func mapPoint(_ p: SIMD2<Float>) -> CGPoint {
            let x = CGFloat(p.x - minX) * scale + padding
            let y = CGFloat(p.y - minZ) * scale + padding
            return CGPoint(x: x, y: canvasSize.height - y)
        }

        func mapSegment(_ s: (SIMD2<Float>, SIMD2<Float>)) -> Line {
            Line(start: mapPoint(s.0), end: mapPoint(s.1))
        }

        return Plan(
            walls: wallSegments.map(mapSegment),
            doors: doorSegments.map(mapSegment),
            windows: windowSegments.map(mapSegment)
        )
    }

    private static func segment(from transform: simd_float4x4, length: Float) -> (SIMD2<Float>, SIMD2<Float>) {
        let center = SIMD3<Float>(transform.columns.3.x, transform.columns.3.y, transform.columns.3.z)
        let xAxis = SIMD3<Float>(transform.columns.0.x, transform.columns.0.y, transform.columns.0.z)
        let half = (length / 2)

        let p1 = center - xAxis * half
        let p2 = center + xAxis * half

        return (SIMD2<Float>(p1.x, p1.z), SIMD2<Float>(p2.x, p2.z))
    }
}
