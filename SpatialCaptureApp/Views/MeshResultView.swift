import SceneKit
import SwiftUI

struct MeshResultView: UIViewRepresentable {
    let meshSnapshots: [ARMeshSnapshot]

    func makeUIView(context: Context) -> SCNView {
        let view = SCNView(frame: .zero)
        view.scene = SCNScene()
        view.backgroundColor = .black
        view.allowsCameraControl = true
        view.autoenablesDefaultLighting = true
        view.rendersContinuously = false
        return view
    }

    func updateUIView(_ uiView: SCNView, context: Context) {
        let scene = SCNScene()
        scene.rootNode.addChildNode(makeWorldAxesNode())

        for snapshot in meshSnapshots {
            scene.rootNode.addChildNode(makeMeshNode(snapshot: snapshot))
        }

        uiView.scene = scene
    }

    private func makeMeshNode(snapshot: ARMeshSnapshot) -> SCNNode {
        let vertices = snapshot.vertices.map { SCNVector3($0.x, $0.y, $0.z) }
        let source = SCNGeometrySource(vertices: vertices)

        let indexData = Data(bytes: snapshot.triangleIndices, count: snapshot.triangleIndices.count * MemoryLayout<UInt32>.size)
        let element = SCNGeometryElement(data: indexData, primitiveType: .triangles, primitiveCount: snapshot.triangleIndices.count / 3, bytesPerIndex: MemoryLayout<UInt32>.size)

        let geometry = SCNGeometry(sources: [source], elements: [element])
        geometry.firstMaterial?.diffuse.contents = UIColor.systemTeal.withAlphaComponent(0.55)
        geometry.firstMaterial?.isDoubleSided = true

        let node = SCNNode(geometry: geometry)
        node.simdTransform = snapshot.transform
        return node
    }

    private func makeWorldAxesNode() -> SCNNode {
        let parent = SCNNode()

        let x = SCNCylinder(radius: 0.003, height: 0.25)
        x.firstMaterial?.diffuse.contents = UIColor.systemRed
        let xNode = SCNNode(geometry: x)
        xNode.eulerAngles = SCNVector3(0, 0, Float.pi / 2)
        xNode.position = SCNVector3(0.125, 0, 0)

        let y = SCNCylinder(radius: 0.003, height: 0.25)
        y.firstMaterial?.diffuse.contents = UIColor.systemGreen
        let yNode = SCNNode(geometry: y)
        yNode.position = SCNVector3(0, 0.125, 0)

        let z = SCNCylinder(radius: 0.003, height: 0.25)
        z.firstMaterial?.diffuse.contents = UIColor.systemBlue
        let zNode = SCNNode(geometry: z)
        zNode.eulerAngles = SCNVector3(Float.pi / 2, 0, 0)
        zNode.position = SCNVector3(0, 0, 0.125)

        parent.addChildNode(xNode)
        parent.addChildNode(yNode)
        parent.addChildNode(zNode)

        return parent
    }
}
