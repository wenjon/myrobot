# -*- coding: utf-8 -*-
"""检查表情/动作三处定义一致：head3d.js(EXPR/OVERLAYS) ↔ main.js(EMOTION_SET/ACTION_MAP) ↔ config.py(SYSTEM_PROMPT)。

三处任一漏改，模型给的标记就会被前端降级丢弃或压根不知道能用，
是这个项目最容易出的低级 bug，所以固化成 CI 检查。
顺带统计 avatar.glb 的 ARKit blendshape 覆盖率。
"""
import re, sys, json, struct, pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
errors = []


def read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def block(text, start_marker, open_ch="{"):
    """从 `start_marker` 后的第一个 open_ch 开始做括号配平，返回块内文本。

    EMOTION_SET 是 `new Set([...])` 用方括号，EXPR/ACTION_MAP 用花括号，
    所以 open_ch 要能切换，否则会一路配平到下一个对象字面量去（曾经踩过）。
    """
    close_ch = {"{": "}", "[": "]"}[open_ch]
    i = text.index(start_marker)
    i = text.index(open_ch, i)
    depth = 0
    for j in range(i, len(text)):
        if text[j] == open_ch:
            depth += 1
        elif text[j] == close_ch:
            depth -= 1
            if depth == 0:
                return text[i + 1 : j]
    raise ValueError("未找到配平的 %s : %s" % (close_ch, start_marker))


head = read("frontend/src/head3d.js")
main = read("frontend/src/main.js")
cfg = read("backend/config.py")

# ---- 1) 各处的名字集合 ----
expr_js = set(re.findall(r"^\s*'([^']+)':\s*[{(]", block(head, "const EXPR ="), re.M))
overlay_js = set(re.findall(r"^\s*'([^']+)':\s*\{", block(head, "const OVERLAYS ="), re.M))

emotion_main = set(re.findall(r"'([^']+)'", block(main, "const EMOTION_SET = new Set(", "[")))
action_main = set(re.findall(r"^\s*'([^']+)':\s*\(h\)", block(main, "const ACTION_MAP ="), re.M))

def prompt_list(label):
    m = re.search(r'"可用' + label + r'：([^"。]+)', cfg)
    if not m:
        errors.append(f"config.py SYSTEM_PROMPT 里找不到「可用{label}：」清单")
        return set()
    return {x.strip() for x in m.group(1).split("、") if x.strip()}

expr_cfg = prompt_list("表情")
action_cfg = prompt_list("动作")


def compare(label, a, a_name, b, b_name):
    for x in sorted(a - b):
        errors.append(f"{label}「{x}」存在于 {a_name}，但 {b_name} 缺失")
    for x in sorted(b - a):
        errors.append(f"{label}「{x}」存在于 {b_name}，但 {a_name} 缺失")


compare("表情", expr_js, "head3d.js EXPR", emotion_main, "main.js EMOTION_SET")
compare("表情", expr_js, "head3d.js EXPR", expr_cfg, "config.py SYSTEM_PROMPT")

# ACTION_MAP 里的动作是 prompt 的超集（允许存在别名，如 对视/看向对方）
for x in sorted(action_cfg - action_main):
    errors.append(f"动作「{x}」写进了 config.py prompt，但 main.js ACTION_MAP 无法分发")

# overlay 动作名必须能被 main.js 分发
for x in sorted(overlay_js - action_main):
    errors.append(f"叠加动作「{x}」定义在 head3d.js OVERLAYS，但 main.js ACTION_MAP 未接")

# ---- 2) blendshape 覆盖率 ----
glb = (ROOT / "frontend/src/avatar.glb").read_bytes()
ln = struct.unpack_from("<I", glb, 12)[0]
gltf = json.loads(glb[20 : 20 + ln].decode("utf-8"))
names = []
for mesh in gltf.get("meshes", []):
    for n in (mesh.get("extras") or {}).get("targetNames", []):
        if n not in names:
            names.append(n)
arkit = [n for n in names if not n.startswith("viseme")]
used = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", head))
missing = [n for n in arkit if n not in used]

# ---- 3) 报告 ----
print(f"表情: {len(expr_js)} 种（三处一致性已比对）")
print(f"动作: main.js {len(action_main)} 个分发项，其中面部叠加 {len(overlay_js)} 个")
print(f"blendshape: ARKit {len(arkit) - len(missing)}/{len(arkit)} 已使用，viseme {len(names) - len(arkit)} 个")
if missing:
    print("  [warn] 未使用的 ARKit blendshape: " + ", ".join(missing))

if errors:
    print("\n发现 %d 处不一致：" % len(errors))
    for e in errors:
        print("  [x] " + e)
    sys.exit(1)
print("\nOK：表情/动作在 head3d.js、main.js、config.py 三处定义一致")
