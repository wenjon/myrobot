# -*- coding: utf-8 -*-
"""探测 glb：blendshape / 骨骼 / 面数 / 贴图，输出与代码要求的匹配情况。"""
import json, struct, sys, io, os, pathlib

path = sys.argv[1] if len(sys.argv) > 1 else r"D:\agentos\myrobot\frontend\src\avatar.glb"
data = pathlib.Path(path).read_bytes()
ln = struct.unpack_from("<I", data, 12)[0]
g = json.loads(data[20:20+ln].decode("utf-8"))

out = io.open(os.path.join(os.environ["TEMP"], "probe_glb.txt"), "w", encoding="utf-8")
def w(s=""): out.write(str(s) + "\n")

w("文件: %s  (%.2f MB)" % (path, len(data)/1024/1024))

# 面数 / 顶点数
tris = 0; verts = 0
accs = g.get("accessors", [])
for m in g.get("meshes", []):
    for p in m.get("primitives", []):
        if "indices" in p: tris += accs[p["indices"]]["count"] // 3
        pos = p.get("attributes", {}).get("POSITION")
        if pos is not None: verts += accs[pos]["count"]
w("网格: %d 个 mesh, %d 三角面, %d 顶点" % (len(g.get("meshes", [])), tris, verts))
w("贴图: %d 张, 材质 %d 个" % (len(g.get("images", [])), len(g.get("materials", []))))
w("动画: %d 个" % len(g.get("animations", [])))

# blendshape
names = []
for m in g.get("meshes", []):
    tn = (m.get("extras") or {}).get("targetNames", [])
    if tn: w("  mesh %-18s morph=%d" % (m.get("name"), len(tn)))
    for n in tn:
        if n not in names: names.append(n)
vis = [n for n in names if n.startswith("viseme")]
ark = [n for n in names if not n.startswith("viseme")]
w("blendshape 合计: %d (viseme %d + 其他 %d)" % (len(names), len(vis), len(ark)))

# 骨骼
nodes = g.get("nodes", [])
joint_ids = set()
for sk in g.get("skins", []): joint_ids.update(sk.get("joints", []))
bones = sorted(nodes[i].get("name", "?") for i in joint_ids)
w("骨骼: %d 根" % len(bones))
NEED_BONES = ["Head", "Neck", "LeftEye", "RightEye", "Spine1"]
w("关键骨骼: " + ", ".join("%s=%s" % (b, "OK" if b in bones else "缺失") for b in NEED_BONES))

# 与代码要求比对
VISEMES = ['viseme_sil','viseme_PP','viseme_FF','viseme_TH','viseme_DD','viseme_kk',
  'viseme_CH','viseme_SS','viseme_nn','viseme_RR','viseme_aa','viseme_E','viseme_I','viseme_O','viseme_U']
miss_v = [v for v in VISEMES if v not in names]
w("viseme 匹配: %d/15 %s" % (15-len(miss_v), ("缺 " + ",".join(miss_v)) if miss_v else "全部齐备"))
out.close()
