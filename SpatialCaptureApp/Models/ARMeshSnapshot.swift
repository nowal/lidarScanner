import ARKit
import simd

struct ARMeshSnapshot: Identifiable {
    let id: UUID
    let transform: simd_float4x4
    let vertices: [SIMD3<Float>]
    let triangleIndices: [UInt32]

    init(anchor: ARMeshAnchor) {
        id = anchor.identifier
        transform = anchor.transform

        let geometry = anchor.geometry
        let vertexBuffer = geometry.vertices
        let indexBuffer = geometry.faces

        var builtVertices: [SIMD3<Float>] = []
        builtVertices.reserveCapacity(vertexBuffer.count)

        for i in 0..<vertexBuffer.count {
            let vertex = vertexBuffer.vertex(at: UInt32(i))
            builtVertices.append(vertex)
        }

        var builtIndices: [UInt32] = []
        let faceCount = indexBuffer.count
        builtIndices.reserveCapacity(faceCount * 3)

        for i in 0..<faceCount {
            let face = indexBuffer.triangleIndices(at: i)
            builtIndices.append(UInt32(face.0))
            builtIndices.append(UInt32(face.1))
            builtIndices.append(UInt32(face.2))
        }

        vertices = builtVertices
        triangleIndices = builtIndices
    }
}

private extension ARGeometrySource {
    func vertex(at index: UInt32) -> SIMD3<Float> {
        let ptr = buffer.contents().advanced(by: offset + stride * Int(index))
        let raw = ptr.assumingMemoryBound(to: (Float, Float, Float).self).pointee
        return SIMD3<Float>(raw.0, raw.1, raw.2)
    }
}

private extension ARGeometryElement {
    func triangleIndices(at faceIndex: Int) -> (UInt32, UInt32, UInt32) {
        precondition(primitiveType == .triangle, "Expected triangle mesh primitive")

        let basePtr = buffer.contents().advanced(by: offset + bytesPerIndex * 3 * faceIndex)

        switch bytesPerIndex {
        case 2:
            let ptr = basePtr.assumingMemoryBound(to: UInt16.self)
            return (UInt32(ptr[0]), UInt32(ptr[1]), UInt32(ptr[2]))
        case 4:
            let ptr = basePtr.assumingMemoryBound(to: UInt32.self)
            return (ptr[0], ptr[1], ptr[2])
        default:
            fatalError("Unsupported bytesPerIndex: \(bytesPerIndex)")
        }
    }
}
