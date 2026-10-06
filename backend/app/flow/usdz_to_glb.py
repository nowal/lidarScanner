"""Lossless geometry/texture derivative for TakeShape's static textured USD exports.

OpenUSD composes nested USDZ references; no hand-written USDA/USDC parser and no
re-meshing, atlas baking, texture resizing, or modification of the source package.
Unsupported scene features fail closed instead of delivering a partial model.
Run in a bounded subprocess, not on the API event loop.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import struct
from pathlib import Path

import numpy as np
from pxr import Ar, Usd, UsdGeom, UsdShade

VERSION = 'usd25.11-glb-v2'


def convert(source: Path, destination: Path) -> dict:
    source = source.resolve()
    stage = Usd.Stage.Open(str(source))
    if not stage or stage.GetCompositionErrors():
        raise ValueError('The USD scene could not be fully composed')
    for layer in stage.GetUsedLayers():
        if not layer.anonymous and not (layer.identifier == str(source) or layer.identifier.startswith(str(source) + '[')):
            raise ValueError('External USD references are not supported')
    doc = {'asset': {'version': '2.0', 'generator': VERSION}, 'scene': 0,
           'scenes': [{'nodes': [0]}], 'nodes': [{'name': 'TakeShape scan', 'children': []}],
           'meshes': [], 'materials': [], 'textures': [], 'images': [], 'samplers': [],
           'accessors': [], 'bufferViews': [], 'buffers': []}
    units = UsdGeom.GetStageMetersPerUnit(stage)
    doc['nodes'][0]['scale'] = [units] * 3
    if UsdGeom.GetStageUpAxis(stage) == 'Z':
        doc['nodes'][0]['rotation'] = [-2 ** -0.5, 0, 0, 2 ** -0.5]
    cache = UsdGeom.XformCache()
    materials, textures = {}, {}
    total_vertices = total_triangles = 0
    bin_path = destination.with_suffix('.bin')
    binary = bin_path.open('wb')

    def view(data):
        binary.write(b'\0' * (-binary.tell() % 4))
        start = binary.tell(); binary.write(data)
        doc['bufferViews'].append({'buffer': 0, 'byteOffset': start, 'byteLength': len(data)})
        return len(doc['bufferViews']) - 1

    def accessor(values, kind, indices=False):
        values = np.asarray(values, dtype='<u4' if indices else '<f4')
        if not indices and not np.isfinite(values).all():
            raise ValueError('Non-finite mesh attribute')
        vi = view(values.tobytes())
        doc['bufferViews'][vi]['target'] = 34963 if indices else 34962
        a = {'bufferView': vi, 'componentType': 5125 if indices else 5126,
             'count': len(values), 'type': kind}
        if kind == 'VEC3':
            a.update(min=values.min(axis=0).tolist(), max=values.max(axis=0).tolist())
        doc['accessors'].append(a)
        return len(doc['accessors']) - 1

    def texture(shader):
        if shader.GetIdAttr().Get() != 'UsdUVTexture':
            raise ValueError('Unsupported texture shader')
        asset = shader.GetInput('file').Get()
        resolved = asset.resolvedPath if asset else ''
        if not resolved.startswith(str(source) + '['):
            raise ValueError('Texture must be embedded in the source USDZ')
        uv_input = shader.GetInput('st')
        uv = uv_input.GetConnectedSource() if uv_input else None
        if uv:
            reader = UsdShade.Shader(uv[0].GetPrim())
            if reader.GetIdAttr().Get() != 'UsdPrimvarReader_float2' or reader.GetInput('varname').Get() != 'st':
                raise ValueError('Unsupported texture coordinate mapping')
        for key, default in [('scale', (1, 1, 1, 1)), ('bias', (0, 0, 0, 0))]:
            v = shader.GetInput(key).Get()
            if v is not None and tuple(v) != default:
                raise ValueError('Unsupported texture color transform')
        wrap = {'clamp': 33071, 'repeat': 10497, 'mirror': 33648, 'useMetadata': 10497}
        wraps = tuple(wrap.get(str(shader.GetInput(k).Get() or 'repeat')) for k in ('wrapS', 'wrapT'))
        if None in wraps:
            raise ValueError('Unsupported texture wrap mode')
        key = (resolved, wraps)
        if key in textures: return textures[key]
        data = bytes(Ar.GetResolver().OpenAsset(Ar.ResolvedPath(resolved)).GetBuffer())
        if data.startswith(b'\x89PNG'): mime = 'image/png'
        elif data.startswith(b'\xff\xd8'): mime = 'image/jpeg'
        else: raise ValueError('Unsupported embedded texture type')
        doc['images'].append({'bufferView': view(data), 'mimeType': mime})
        doc['samplers'].append({'wrapS': wraps[0], 'wrapT': wraps[1], 'magFilter': 9729, 'minFilter': 9729})
        doc['textures'].append({'source': len(doc['images']) - 1, 'sampler': len(doc['samplers']) - 1})
        textures[key] = len(doc['textures']) - 1
        return textures[key]

    def material(prim):
        mat = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial()[0]
        key = str(mat.GetPath()) if mat else ''
        double_sided = bool(UsdGeom.Mesh(prim).GetDoubleSidedAttr().Get()) if prim.IsA(UsdGeom.Mesh) else False
        if (key, double_sided) in materials: return materials[key, double_sided]
        result = {'name': key.rsplit('/', 1)[-1], 'doubleSided': double_sided,
                  'pbrMetallicRoughness': {'metallicFactor': 0, 'roughnessFactor': 1}}
        if mat:
            shader = mat.ComputeSurfaceSource()[0]
            if not shader or shader.GetIdAttr().Get() != 'UsdPreviewSurface':
                raise ValueError('Unsupported material surface')
            pbr = result['pbrMetallicRoughness']
            for inp in shader.GetInputs():
                name = str(inp.GetBaseName())
                conn = inp.GetConnectedSource()
                if conn and name not in ('diffuseColor', 'emissiveColor', 'normal'):
                    raise ValueError('Unsupported material texture channel: ' + name)
            def color(name, target, texture_target, container, default):
                inp = shader.GetInput(name); conn = inp.GetConnectedSource() if inp else None
                val = list(inp.Get() or default)
                if conn:
                    val = [1, 1, 1]
                    container[texture_target] = {'index': texture(UsdShade.Shader(conn[0].GetPrim()))}
                container[target] = val
            color('diffuseColor', 'baseColorFactor', 'baseColorTexture', pbr, [0.18] * 3)
            opacity = shader.GetInput('opacity').Get()
            pbr['baseColorFactor'].append(float(opacity if opacity is not None else 1))
            color('emissiveColor', 'emissiveFactor', 'emissiveTexture', result, [0] * 3)
            for inp, out, default in [('metallic', 'metallicFactor', 0), ('roughness', 'roughnessFactor', 0.5)]:
                v = shader.GetInput(inp).Get(); pbr[out] = float(v if v is not None else default)
            if shader.GetInput('useSpecularWorkflow').Get():
                raise ValueError('Specular workflow requires a different converter')
            normal_input = shader.GetInput('normal')
            normal = normal_input.GetConnectedSource() if normal_input else None
            if normal:
                # USD normal textures need scale/bias/channel translation; fail safely.
                raise ValueError('Normal textures require a different converter')
            threshold = shader.GetInput('opacityThreshold').Get() or 0
            if threshold:
                result.update(alphaMode='MASK', alphaCutoff=float(threshold))
            elif pbr['baseColorFactor'][3] < 1:
                result['alphaMode'] = 'BLEND'
        doc['materials'].append(result)
        materials[key, double_sided] = len(doc['materials']) - 1
        return len(doc['materials']) - 1

    try:
        for prim in Usd.PrimRange.Stage(stage, Usd.TraverseInstanceProxies()):
            if not prim.IsA(UsdGeom.Mesh):
                if prim.IsA(UsdGeom.PointBased): raise ValueError('Unsupported non-mesh geometry')
                continue
            mesh = UsdGeom.Mesh(prim)
            if mesh.ComputeVisibility() == 'invisible': continue
            if any(a.GetNumTimeSamples() for a in prim.GetAttributes()):
                raise ValueError('Animated geometry is not a static scan')
            counts = np.asarray(mesh.GetFaceVertexCountsAttr().Get(), dtype=np.int64)
            if not len(counts): continue
            if np.any(counts != 3) or len(mesh.GetHoleIndicesAttr().Get() or []):
                raise ValueError('Only triangulated scan surfaces are supported')
            points = np.asarray(mesh.GetPointsAttr().Get(), dtype=np.float32)
            faces = np.asarray(mesh.GetFaceVertexIndicesAttr().Get(), dtype=np.int64)
            if len(faces) != len(counts) * 3 or faces.min() < 0 or faces.max() >= len(points):
                raise ValueError('Invalid mesh indices')
            normals = np.asarray(mesh.GetNormalsAttr().Get(), dtype=np.float32) if mesh.GetNormalsAttr().Get() else None
            uv = UsdGeom.PrimvarsAPI(prim).GetPrimvar('st')
            uvs = np.asarray(uv.ComputeFlattened(), dtype=np.float32) if uv and uv.HasValue() else None
            expanded = (normals is not None and mesh.GetNormalsInterpolation() not in ('vertex', 'varying')) or (uvs is not None and uv.GetInterpolation() not in ('vertex', 'varying'))
            def attribute(values, interpolation):
                if values is None: return None
                if interpolation in ('vertex', 'varying'): return values[faces] if expanded else values
                if interpolation == 'faceVarying': return values
                if interpolation == 'uniform': return np.repeat(values, 3, axis=0)
                if interpolation == 'constant': return np.repeat(values[:1], len(faces), axis=0)
                raise ValueError('Unsupported attribute interpolation')
            p = points[faces] if expanded else points
            n = attribute(normals, mesh.GetNormalsInterpolation())
            t = attribute(uvs, uv.GetInterpolation()) if uvs is not None else None
            attrs = {'POSITION': accessor(p, 'VEC3')}
            if n is not None:
                if len(n) != len(p): raise ValueError('Mismatched normals')
                # Apple trim/interpolation can leave normals with lengths other
                # than one. glTF requires unit normals; preserve their directions.
                n = n.copy()
                lengths = np.linalg.norm(n, axis=1)
                zero = lengths < 1e-12
                if zero.any():
                    tri = points[faces.reshape(-1, 3)]
                    face_normals = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
                    if expanded:
                        fallback = np.repeat(face_normals, 3, axis=0)
                    else:
                        fallback = np.zeros_like(points)
                        for corner in range(3): np.add.at(fallback, faces.reshape(-1, 3)[:, corner], face_normals)
                    n[zero] = fallback[zero]
                    lengths = np.linalg.norm(n, axis=1)
                    n[lengths < 1e-12] = [0, 1, 0]
                    lengths = np.linalg.norm(n, axis=1)
                n /= lengths[:, None]
                attrs['NORMAL'] = accessor(n, 'VEC3')
            if t is not None:
                if len(t) != len(p): raise ValueError('Mismatched texture coordinates')
                t = t.copy(); t[:, 1] = 1 - t[:, 1]
                attrs['TEXCOORD_0'] = accessor(t, 'VEC2')
            indices = np.arange(len(faces)) if expanded else faces.copy()
            indices = indices.reshape((-1, 3))
            if mesh.GetOrientationAttr().Get() == 'leftHanded': indices = indices[:, [0, 2, 1]]
            subsets = UsdShade.MaterialBindingAPI(prim).GetMaterialBindSubsets()
            groups, assigned = [], set()
            for subset in subsets:
                selected = np.asarray(subset.GetIndicesAttr().Get(), dtype=np.int64)
                if any(int(i) in assigned or i < 0 or i >= len(counts) for i in selected): raise ValueError('Invalid material subset')
                assigned.update(map(int, selected)); groups.append((selected, subset.GetPrim()))
            remainder = np.array([i for i in range(len(counts)) if i not in assigned]) if subsets else np.arange(len(counts))
            if len(remainder): groups.append((remainder, prim))
            primitives = []
            for selected, bound in groups:
                mi = material(bound)
                if t is None and (any(k.endswith('Texture') for k in doc['materials'][mi]['pbrMetallicRoughness']) or 'emissiveTexture' in doc['materials'][mi]):
                    raise ValueError('Textured mesh lacks UVs')
                primitives.append({'attributes': attrs, 'indices': accessor(indices[selected].reshape(-1), 'SCALAR', True), 'material': mi})
            doc['meshes'].append({'name': prim.GetName(), 'primitives': primitives})
            doc['nodes'][0]['children'].append(len(doc['nodes']))
            doc['nodes'].append({'name': str(prim.GetPath()), 'mesh': len(doc['meshes']) - 1,
                                 'matrix': np.asarray(cache.GetLocalToWorldTransform(prim)).reshape(-1).tolist()})
            if np.array_equal(np.asarray(cache.GetLocalToWorldTransform(prim)), np.eye(4)):
                del doc['nodes'][-1]['matrix']
            total_vertices += len(p); total_triangles += len(counts)
        if not doc['meshes']: raise ValueError('No visible mesh in the USD scene')
        binary.write(b'\0' * (-binary.tell() % 4)); length = binary.tell(); binary.close()
        doc['buffers'] = [{'byteLength': length}]
        for key in ('materials', 'textures', 'images', 'samplers'):
            if not doc[key]: del doc[key]
        raw = json.dumps(doc, separators=(',', ':'), allow_nan=False).encode(); raw += b' ' * (-len(raw) % 4)
        with destination.open('wb') as output, bin_path.open('rb') as buf:
            output.write(struct.pack('<III', 0x46546C67, 2, 12 + 8 + len(raw) + 8 + length))
            output.write(struct.pack('<I4s', len(raw), b'JSON')); output.write(raw)
            output.write(struct.pack('<I4s', length, b'BIN\0')); shutil.copyfileobj(buf, output, 1024 * 1024)
        return {'meshes': len(doc['meshes']), 'vertices': total_vertices, 'triangles': total_triangles,
                'textures': len(doc.get('textures', [])), 'metersPerUnit': units, 'bytes': destination.stat().st_size}
    finally:
        binary.close(); bin_path.unlink(missing_ok=True)


if __name__ == '__main__':
    import sys
    if sys.platform == 'linux':
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (3 * 1024**3, 3 * 1024**3))
        resource.setrlimit(resource.RLIMIT_CPU, (1100, 1100))
    print(json.dumps(convert(Path(sys.argv[1]), Path(sys.argv[2]))))
