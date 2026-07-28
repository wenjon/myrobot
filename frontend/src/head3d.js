// head3d.js —— 3D 数字人：加载 glb，驱动 Oculus viseme 口型 + ARKit 表情 blendshape。
import * as THREE from 'three';
import { GLTFLoader } from './GLTFLoader.js';

export async function createHead3D(canvas, avatarUrl) {
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x0f1220);

  const cam = new THREE.PerspectiveCamera(20, canvas.clientWidth / canvas.clientHeight, 0.1, 100);
  cam.position.set(0, 1.55, 1.15);
  cam.lookAt(0, 1.55, 0);

  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
  renderer.setPixelRatio(window.devicePixelRatio || 1);
  renderer.outputColorSpace = THREE.SRGBColorSpace;

  // 灯光
  scene.add(new THREE.HemisphereLight(0xffffff, 0x444466, 1.2));
  const key = new THREE.DirectionalLight(0xffffff, 1.6);
  key.position.set(1, 2, 2); scene.add(key);
  const fill = new THREE.DirectionalLight(0x99bbff, 0.5);
  fill.position.set(-2, 1, 1); scene.add(fill);

  function resize() {
    const w = canvas.clientWidth, h = canvas.clientHeight;
    renderer.setSize(w, h, false);
    cam.aspect = w / h; cam.updateProjectionMatrix();
  }
  window.addEventListener('resize', resize);

  // 收集所有带 morphTarget 的 mesh + 名称->索引 字典
  const morphMeshes = [];
  const dict = {}; // name -> [{mesh, index}]
  const loader = new GLTFLoader();
  const gltf = await loader.loadAsync(avatarUrl);
  const root = gltf.scene;
  root.traverse((o) => {
    if (o.isMesh && o.morphTargetDictionary && o.morphTargetInfluences) {
      morphMeshes.push(o);
      for (const [name, idx] of Object.entries(o.morphTargetDictionary)) {
        (dict[name] ||= []).push({ mesh: o, index: idx });
      }
    }
  });
  scene.add(root);

  // 相机对准头部：优先用 Head 骨骼世界坐标，取不到再退化到包围盒顶部
  root.updateWorldMatrix(true, true);
  let headBone = null;
  root.traverse((o) => { if (o.isBone && o.name === 'Head') headBone = o; });
  const headPos = new THREE.Vector3();
  if (headBone) {
    headBone.getWorldPosition(headPos);
    headPos.y += 0.08; // 骨骼在下颌附近，略上移到脸中心
  } else {
    const box = new THREE.Box3().setFromObject(root);
    box.getCenter(headPos);
    headPos.y = box.max.y - 0.15;
  }
  // 头部大约 0.20~0.24m，用一点余量取景（含头发/下巴）
  const dist = 0.62;
  cam.fov = 24; cam.updateProjectionMatrix();
  cam.position.set(headPos.x, headPos.y, headPos.z + dist);
  cam.lookAt(headPos.x, headPos.y, headPos.z);

  // ---- blendshape 目标值与当前值（做平滑插值）----
  const target = {};   // name -> 0..1 目标
  const current = {};  // name -> 0..1 当前
  function setBS(name, v) { target[name] = Math.max(0, Math.min(1, v)); }
  function ensure(name) { if (!(name in current)) current[name] = 0; }

  // 应用到所有 mesh
  function apply(name, v) {
    const arr = dict[name];
    if (!arr) return;
    for (const { mesh, index } of arr) mesh.morphTargetInfluences[index] = v;
  }

  // ---------- 对外接口 ----------
  const VISEMES = ['viseme_sil','viseme_PP','viseme_FF','viseme_TH','viseme_DD','viseme_kk',
    'viseme_CH','viseme_SS','viseme_nn','viseme_RR','viseme_aa','viseme_E','viseme_I','viseme_O','viseme_U'];

  // 设置口型：给定一个 viseme 名与强度，其余 viseme 归零
  function setViseme(name, intensity = 1.0) {
    for (const v of VISEMES) setBS(v, v === name ? intensity : 0);
    // jawOpen 跟随张口类 viseme
    const openish = ['viseme_aa','viseme_O','viseme_U','viseme_E','viseme_CH','viseme_DD','viseme_kk','viseme_RR'];
    setBS('jawOpen', openish.includes(name) ? intensity * 0.5 : 0.02);
  }
  function mouthClosed() {
    for (const v of VISEMES) setBS(v, v === 'viseme_sil' ? 0.0 : 0);
    setBS('jawOpen', 0.0);
  }

  // 表情：喜怒哀乐 -> blendshape 组合
  const EXPR = {
    '平静':  {},
    '开心':  { mouthSmileLeft: 0.8, mouthSmileRight: 0.8, cheekSquintLeft: 0.4, cheekSquintRight: 0.4, browInnerUp: 0.15 },
    '悲伤':  { mouthFrownLeft: 0.7, mouthFrownRight: 0.7, browInnerUp: 0.7, eyeSquintLeft: 0.3, eyeSquintRight: 0.3 },
    '生气':  { browDownLeft: 0.9, browDownRight: 0.9, noseSneerLeft: 0.5, noseSneerRight: 0.5, mouthPressLeft: 0.5, mouthPressRight: 0.5 },
    '惊讶':  { browInnerUp: 0.8, browOuterUpLeft: 0.7, browOuterUpRight: 0.7, eyeWideLeft: 0.8, eyeWideRight: 0.8, jawOpen: 0.3 },
    '疑惑':  { browInnerUp: 0.5, browOuterUpLeft: 0.6, mouthLeft: 0.4, eyeSquintLeft: 0.3 },
  };
  const EXPR_KEYS = new Set(Object.values(EXPR).flatMap(o => Object.keys(o)));
  let currentExpr = '平静';
  function setExpression(name) {
    if (!EXPR[name]) return;
    currentExpr = name;
    const set = EXPR[name];
    for (const k of EXPR_KEYS) setBS(k, set[k] || 0);
  }

  // 点头 / 摇头（旋转整个 root 的头部；简化为绕整体）
  let nod = 0, shake = 0;
  function triggerNod() { nod = 1; }
  function triggerShake() { shake = 1; }

  // 眨眼
  let nextBlink = performance.now() + 1500, blink = 0;

  const clock = new THREE.Clock();
  function loop() {
    const dt = clock.getDelta();
    const now = performance.now();

    // 眨眼
    if (now > nextBlink) { blink = 1; nextBlink = now + 1800 + Math.random() * 2500; }
    blink = Math.max(0, blink - dt * 8);
    setBS('eyeBlinkLeft', blink); setBS('eyeBlinkRight', blink);

    // 平滑逼近所有目标
    const speed = 12 * dt;
    const keys = new Set([...Object.keys(target), ...Object.keys(current)]);
    for (const k of keys) {
      ensure(k);
      const t = target[k] || 0;
      current[k] += (t - current[k]) * Math.min(1, speed);
      apply(k, current[k]);
    }

    // 点头/摇头
    if (nod > 0) { nod = Math.max(0, nod - dt * 2.2); root.rotation.x = Math.sin(nod * Math.PI) * 0.18; }
    else root.rotation.x += (0 - root.rotation.x) * 0.2;
    if (shake > 0) { shake = Math.max(0, shake - dt * 2.2); root.rotation.y = Math.sin(shake * Math.PI * 2) * 0.18; }
    else root.rotation.y += (0 - root.rotation.y) * 0.2;

    renderer.render(scene, cam);
    requestAnimationFrame(loop);
  }
  resize();
  mouthClosed();
  requestAnimationFrame(loop);

  return { setViseme, mouthClosed, setExpression, triggerNod, triggerShake };
}



