"""Conversion fixtures exercise composed packages, not a standalone sample mesh."""
import hashlib
import io
import json
import struct
import zipfile

import numpy as np
import pytest
from PIL import Image
from pxr import Usd, UsdGeom, UsdShade, Sdf, Gf
from app.flow.usdz_to_glb import convert


def package(path, members):
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_STORED) as z:
        for name, data in members.items(): z.writestr(name, data)


def document(path):
    data = path.read_bytes(); length = struct.unpack_from('<I', data, 12)[0]
    return json.loads(data[20:20+length]), data[28+length:]


def values(doc, binary, accessor):
    a = doc['accessors'][accessor]; v = doc['bufferViews'][a['bufferView']]
    return np.frombuffer(binary[v.get('byteOffset', 0):v.get('byteOffset', 0)+v['byteLength']], dtype='<u4' if a['componentType'] == 5125 else '<f4')


def make_nested(tmp_path):
    stage = Usd.Stage.CreateNew(str(tmp_path/'mesh.usda'))
    root = UsdGeom.Xform.Define(stage, '/Scan'); stage.SetDefaultPrim(root.GetPrim())
    mesh = UsdGeom.Mesh.Define(stage, '/Scan/Mesh')
    mesh.CreatePointsAttr([(0,0,0),(1,0,0),(0,1,0)])
    mesh.CreateFaceVertexCountsAttr([3]); mesh.CreateFaceVertexIndicesAttr([0,1,2])
    mesh.CreateNormalsAttr([(0,0,2)]*3); mesh.SetNormalsInterpolation('vertex')
    mesh.CreateSubdivisionSchemeAttr('none'); mesh.CreateDoubleSidedAttr(True)
    uv = UsdGeom.PrimvarsAPI(mesh).CreatePrimvar('st', Sdf.ValueTypeNames.TexCoord2fArray, 'faceVarying')
    uv.Set([(0,0),(1,0),(0,1)]); uv.SetIndices([0,1,2])
    mat = UsdShade.Material.Define(stage, '/Scan/Material')
    sh = UsdShade.Shader.Define(stage, '/Scan/Material/Surface'); sh.CreateIdAttr('UsdPreviewSurface')
    sh.CreateInput('diffuseColor',Sdf.ValueTypeNames.Color3f).Set((0,0,0))
    sh.CreateInput('roughness',Sdf.ValueTypeNames.Float).Set(0.7)
    tex = UsdShade.Shader.Define(stage, '/Scan/Material/Texture'); tex.CreateIdAttr('UsdUVTexture')
    tex.CreateInput('file', Sdf.ValueTypeNames.Asset).Set(Sdf.AssetPath('texture.png'))
    tex.CreateOutput('rgb',Sdf.ValueTypeNames.Float3)
    sh.CreateInput('emissiveColor',Sdf.ValueTypeNames.Color3f).ConnectToSource(tex.ConnectableAPI(),'rgb')
    sh.CreateOutput('surface', Sdf.ValueTypeNames.Token)
    mat.CreateSurfaceOutput().ConnectToSource(sh.ConnectableAPI(),'surface')
    UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim()).Bind(mat)
    stage.GetRootLayer().Save()
    image = io.BytesIO(); Image.new('RGB',(8,8),'orange').save(image,'PNG')
    package(tmp_path/'inner.usdz', {'mesh.usda':(tmp_path/'mesh.usda').read_bytes(),'texture.png':image.getvalue()})
    package(tmp_path/'area.usdz', {'area.usda':b'#usda 1.0\n(defaultPrim="Area")\ndef Xform "Area" (references=@./parts/model.usdz@) {}\n', 'parts/model.usdz':(tmp_path/'inner.usdz').read_bytes()})
    root = b'''#usda 1.0
(metersPerUnit=0.01\nupAxis="Z")
def Xform "Room" (references=@./rooms/room-1/model.usdz@) {
 double3 xformOp:translate = (3,4,5)
 double3 xformOp:scale = (2,3,4)
 uniform token[] xformOpOrder = ["xformOp:translate", "xformOp:scale"]
}
'''
    package(tmp_path/'home.usdz', {'home.usda':root,'rooms/room-1/model.usdz':(tmp_path/'area.usdz').read_bytes()})
    return tmp_path/'home.usdz', image.getvalue()


def test_nested_material_texture_uv_geometry_and_transforms_preserved(tmp_path):
    source, texture = make_nested(tmp_path)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    target=tmp_path/'model.glb'; stats = convert(source,target)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert stats['meshes']==1 and stats['triangles']==1 and stats['textures']==1
    doc,binary = document(target)
    assert doc['nodes'][0]['scale']==[0.01]*3
    assert doc['nodes'][0]['rotation']==pytest.approx([-2**-0.5,0,0,2**-0.5])
    matrix=np.asarray(doc['nodes'][1]['matrix']).reshape(4,4)
    assert matrix[3,:3].tolist()==[3,4,5]
    assert np.diag(matrix)[:3].tolist()==[2,3,4]
    p=doc['meshes'][0]['primitives'][0]
    assert values(doc,binary,p['attributes']['POSITION']).tolist()==[0,0,0,1,0,0,0,1,0]
    assert values(doc,binary,p['attributes']['TEXCOORD_0']).tolist()==[0,1,1,1,0,0]
    assert values(doc,binary,p['indices']).tolist()==[0,1,2]
    assert values(doc,binary,p['attributes']['NORMAL']).tolist()==[0,0,1]*3
    v=doc['bufferViews'][doc['images'][0]['bufferView']]
    assert binary[v['byteOffset']:v['byteOffset']+v['byteLength']]==texture
    material=doc['materials'][0]
    assert material['emissiveFactor']==[1,1,1]
    assert material['pbrMetallicRoughness']['baseColorFactor']==[0,0,0,1]
    assert material['doubleSided'] is True
    assert material['pbrMetallicRoughness']['roughnessFactor']==pytest.approx(.7)


def test_missing_reference_fails_instead_of_partial_preview(tmp_path):
    source=tmp_path/'bad.usdz'
    package(source,{'root.usda':b'#usda 1.0\ndef Xform "Missing" (references=@./missing.usdz@) {}\n'})
    with pytest.raises(ValueError):convert(source,tmp_path/'bad.glb')


def test_external_reference_cannot_embed_server_files(tmp_path):
    source,_=make_nested(tmp_path)
    bad=tmp_path/'external.usdz'
    package(bad,{'root.usda':f'#usda 1.0\ndef Xform "External" (references=@{tmp_path}/mesh.usda@) {{}}\n'.encode()})
    with pytest.raises(ValueError,match='External'):convert(bad,tmp_path/'bad.glb')
