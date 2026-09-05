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
  // 抓取头部相关骨骼：颈/头用于分层转头，双眼骨用于眼球微转（配合 eyeLook* blendshape），
  // Spine 用于呼吸起伏。Ready Player Me 标准骨架必然带这几根。
  let headBone = null, neckBone = null, eyeLBone = null, eyeRBone = null, spineBone = null;
  root.traverse((o) => {
    if (!o.isBone) return;
    if (o.name === 'Head') headBone = o;
    else if (o.name === 'Neck') neckBone = o;
    else if (o.name === 'LeftEye') eyeLBone = o;
    else if (o.name === 'RightEye') eyeRBone = o;
    else if (o.name === 'Spine1') spineBone = o;
  });
  // 记录静止姿态，所有旋转都是在静止值上做增量，避免累积漂移。
  const rest = new Map();
  for (const b of [headBone, neckBone, eyeLBone, eyeRBone, spineBone]) {
    if (b) rest.set(b, b.rotation.clone());
  }
  function setBoneRot(bone, dx, dy, dz) {
    if (!bone) return;
    const r = rest.get(bone);
    bone.rotation.set(r.x + dx, r.y + dy, r.z + dz);
  }
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

  // 表情：喜怒哀乐 -> blendshape 组合。
  //
  // 设计原则（52 个 ARKit blendshape 的分工）：
  //   · 眼动 8 个（eyeLook*）+ 眨眼 2 个（eyeBlink*）交给独立系统（见 lookAt / 眨眼），
  //     不参与表情组合，避免"表情覆盖注视"或"注视覆盖眨眼"的互相打架；
  //   · 其余 42 个用于下面的表情表，每个表情是一组 blendshape 的加权叠加；
  //   · 不对称是关键：真人表情很少左右完全一致，单侧抬眉/单侧嘴角能显著提升可信度。
  const EXPR = {
    // ---- 基础 6 种（原有，微调加了酒窝/嘴角细节让过渡更自然）----
    '平静':  {},
    '开心':  { mouthSmileLeft: 0.8, mouthSmileRight: 0.8, mouthDimpleLeft: 0.4, mouthDimpleRight: 0.4,
               cheekSquintLeft: 0.4, cheekSquintRight: 0.4, browInnerUp: 0.15, eyeSquintLeft: 0.2, eyeSquintRight: 0.2 },
    '悲伤':  { mouthFrownLeft: 0.7, mouthFrownRight: 0.7, browInnerUp: 0.7, eyeSquintLeft: 0.3, eyeSquintRight: 0.3,
               mouthShrugLower: 0.3, jawOpen: 0.05 },
    '生气':  { browDownLeft: 0.9, browDownRight: 0.9, noseSneerLeft: 0.5, noseSneerRight: 0.5,
               mouthPressLeft: 0.5, mouthPressRight: 0.5, jawForward: 0.2, eyeSquintLeft: 0.35, eyeSquintRight: 0.35 },
    '惊讶':  { browInnerUp: 0.8, browOuterUpLeft: 0.7, browOuterUpRight: 0.7,
               eyeWideLeft: 0.8, eyeWideRight: 0.8, jawOpen: 0.35, mouthFunnel: 0.25 },
    '疑惑':  { browInnerUp: 0.5, browOuterUpLeft: 0.6, mouthLeft: 0.4, eyeSquintLeft: 0.3,
               mouthPressLeft: 0.25, jawLeft: 0.1 },

    // ---- 新增 12 种（把闲置的 36 个 blendshape 用起来）----
    // 害羞：微笑 + 眼睛下垂 + 颊部收紧（脸红做不了，用挤压近似）
    '害羞':  { mouthSmileLeft: 0.45, mouthSmileRight: 0.45, cheekSquintLeft: 0.6, cheekSquintRight: 0.6,
               eyeSquintLeft: 0.4, eyeSquintRight: 0.4, browInnerUp: 0.3, mouthShrugUpper: 0.2 },
    // 调皮：单侧坏笑 + 单眼眯 + 舌尖轻吐
    '调皮':  { mouthSmileLeft: 0.75, mouthSmileRight: 0.3, mouthDimpleLeft: 0.5,
               eyeSquintLeft: 0.55, browOuterUpRight: 0.4, tongueOut: 0.25, jawLeft: 0.08 },
    // 无语：撇嘴 + 眉压低 + 眼往上（白眼由 lookAt 补，这里只做面部）
    '无语':  { mouthShrugUpper: 0.6, mouthPressLeft: 0.5, mouthPressRight: 0.35,
               browDownLeft: 0.35, browDownRight: 0.35, eyeSquintLeft: 0.3, eyeSquintRight: 0.3,
               mouthRight: 0.2, jawRight: 0.1, mouthClose: 0.25 },
    // 思考：单侧抬眉 + 咬唇 + 嘴角偏移
    '思考':  { browOuterUpLeft: 0.55, browInnerUp: 0.25, mouthRollLower: 0.45,
               mouthPressRight: 0.3, mouthLeft: 0.25, eyeSquintRight: 0.2 },
    // 尴尬：嘴角横拉 + 眉心抬 + 眼眯（苦笑）
    '尴尬':  { mouthStretchLeft: 0.6, mouthStretchRight: 0.6, browInnerUp: 0.55,
               eyeSquintLeft: 0.45, eyeSquintRight: 0.45, mouthSmileLeft: 0.2, mouthSmileRight: 0.2 },
    // 得意：双侧上扬 + 下巴前伸 + 眉压低（自信的坏笑）
    '得意':  { mouthSmileLeft: 0.7, mouthSmileRight: 0.7, mouthDimpleLeft: 0.5, mouthDimpleRight: 0.5,
               jawForward: 0.3, browDownLeft: 0.25, browDownRight: 0.25, eyeSquintLeft: 0.4, eyeSquintRight: 0.4 },
    // 委屈：嘴角下垂 + 眉心高抬 + 下唇前撅
    '委屈':  { mouthFrownLeft: 0.6, mouthFrownRight: 0.6, browInnerUp: 0.85,
               mouthPucker: 0.4, mouthShrugLower: 0.5, eyeWideLeft: 0.25, eyeWideRight: 0.25 },
    // 惊恐：眼睛全睁 + 嘴大张 + 眉全抬（比"惊讶"更强烈）
    '惊恐':  { eyeWideLeft: 1.0, eyeWideRight: 1.0, browInnerUp: 0.95,
               browOuterUpLeft: 0.85, browOuterUpRight: 0.85, jawOpen: 0.6,
               mouthStretchLeft: 0.4, mouthStretchRight: 0.4 },
    // 厌恶：鼻翼皱起 + 上唇抬 + 单侧眼眯
    '厌恶':  { noseSneerLeft: 0.8, noseSneerRight: 0.8, mouthUpperUpLeft: 0.6, mouthUpperUpRight: 0.45,
               eyeSquintLeft: 0.55, eyeSquintRight: 0.4, browDownLeft: 0.4, browDownRight: 0.4, mouthLeft: 0.2 },
    // 困倦：眼半闭 + 嘴微张 + 眉松弛
    '困倦':  { eyeBlinkLeft: 0.55, eyeBlinkRight: 0.55, jawOpen: 0.2,
               mouthShrugLower: 0.25, browInnerUp: 0.15, mouthLowerDownLeft: 0.2, mouthLowerDownRight: 0.2 },
    // 撒娇：鼓腮 + 撅嘴 + 眉心抬（卖萌）
    '撒娇':  { cheekPuff: 0.6, mouthPucker: 0.65, browInnerUp: 0.5,
               eyeSquintLeft: 0.25, eyeSquintRight: 0.25, mouthShrugUpper: 0.3 },
    // 期待：眼睛睁大 + 微笑 + 眉抬（兴奋等待）
    '期待':  { eyeWideLeft: 0.55, eyeWideRight: 0.55, mouthSmileLeft: 0.55, mouthSmileRight: 0.55,
               browInnerUp: 0.45, browOuterUpLeft: 0.35, browOuterUpRight: 0.35,
               mouthDimpleLeft: 0.3, mouthDimpleRight: 0.3, jawOpen: 0.12 },
  };
  // 表情系统"拥有"的 blendshape 集合。切表情时先把这些全归零再套新值，
  // 避免上一个表情的残留（比如从"惊恐"切到"平静"时眼睛还睁着）。
  // 注意：不含 eyeLook*（注视系统独占），eyeBlink* 虽被"困倦"用到，
  // 但眨眼在 loop 里每帧覆写，两者是叠加关系不冲突。
  const EXPR_KEYS = new Set(Object.values(EXPR).flatMap(o => Object.keys(o)));
  let currentExpr = '平静';
  // 当前表情的基准值：叠加层（挑眉/鼓腮等瞬时动作）在此之上相加，动作结束后回落到基准，
  // 不会把表情本身"吃掉"。
  let exprBase = {};
  function setExpression(name) {
    if (!EXPR[name]) return;
    currentExpr = name;
    const set = EXPR[name];
    exprBase = set;
    for (const k of EXPR_KEYS) setBS(k, set[k] || 0);
  }
  // 在表情基准值之上叠加一个瞬时增量（0..1 会被裁剪）
  function addBS(name, delta) { setBS(name, (exprBase[name] || 0) + delta); }
  function restoreBS(names) { for (const k of names) setBS(k, exprBase[k] || 0); }
  // 供外部查询/遍历（调试面板用）
  function expressionNames() { return Object.keys(EXPR); }

  // “我在听”倾听表情：被用户打断时切换，体验上像真人收住话、歪头倾听。
  // 组合：眉根微抬 + 双眼微睁大 + 嘴微闭 + 头部轻微侧倾（rotation.z）。
  const LISTEN = { browInnerUp: 0.3, eyeWideLeft: 0.2, eyeWideRight: 0.2 };
  function setListening(on) {
    if (on) {
      for (const k of Object.keys(LISTEN)) setBS(k, LISTEN[k]);
      mouthClosed();
      setPose(0.10, -0.04, 0.13, 6000);   // 微微侧头凑近听
      lookAt(0, 0.05, 6000);
    } else {
      // 恢复到当前表情（setExpression 会重置所有表情相关 blendshape）
      setExpression(currentExpr);
      resetPose();
    }
  }

  // ---------- 眼球注视系统 ----------
  // 坐标约定：x = +1 看向"画面右侧"（= 数字人自己的左侧），y = +1 向上。范围 [-1, 1]。
  // 左右眼的 In/Out 是镜像关系：看画面右侧 = 左眼向外 + 右眼向内。
  const EYE_LOOK = ['eyeLookInLeft','eyeLookOutLeft','eyeLookUpLeft','eyeLookDownLeft',
                    'eyeLookInRight','eyeLookOutRight','eyeLookUpRight','eyeLookDownRight'];
  let gazeTX = 0, gazeTY = 0;   // 目标
  let gazeX = 0, gazeY = 0;     // 当前（平滑逼近）
  let gazeHold = 0;             // >0 表示外部刚指定过注视点，暂停随机扫视
  const clamp1 = (v) => Math.max(-1, Math.min(1, v));
  function lookAt(x, y = 0, holdMs = 1600) {
    gazeTX = clamp1(x); gazeTY = clamp1(y);
    gazeHold = holdMs / 1000;
  }
  function applyGaze() {
    const x = gazeX, y = gazeY;
    // 水平
    setBS('eyeLookOutLeft',  x > 0 ?  x : 0);
    setBS('eyeLookInRight',  x > 0 ?  x : 0);
    setBS('eyeLookInLeft',   x < 0 ? -x : 0);
    setBS('eyeLookOutRight', x < 0 ? -x : 0);
    // 垂直
    setBS('eyeLookUpLeft',    y > 0 ?  y : 0);
    setBS('eyeLookUpRight',   y > 0 ?  y : 0);
    setBS('eyeLookDownLeft',  y < 0 ? -y : 0);
    setBS('eyeLookDownRight', y < 0 ? -y : 0);
    // 眼球骨骼跟着微转，让高光/瞳孔位移更真实（幅度很小，约 ±13°）
    const ey = -x * 0.23, ex = -y * 0.16;
    setBoneRot(eyeLBone, ex, ey, 0);
    setBoneRot(eyeRBone, ex, ey, 0);
  }

  // ---------- 头颈姿态 ----------
  // 持久姿态（转头/抬头/歪头会保持一小会儿再回中）+ 瞬时动作（点头/摇头）叠加。
  // 分层：颈骨承担 60%，头骨承担 40%，看起来是"人在转头"而不是"整个身体歪了"。
  const NECK_SHARE = 0.6, HEAD_SHARE = 0.4;
  let poseYaw = 0, posePitch = 0, poseRoll = 0;          // 目标
  let curYaw = 0, curPitch = 0, curRoll = 0;             // 当前
  let poseHold = 0;                                      // 剩余保持时间（秒），到 0 自动回中
  function setPose(yaw, pitch, roll, holdMs = 1800) {
    poseYaw = yaw; posePitch = pitch; poseRoll = roll;
    poseHold = holdMs / 1000;
  }

  let nod = 0, shake = 0, tilt = 0, tiltDir = 1;
  function triggerNod() { nod = 1; }
  function triggerShake() { shake = 1; }
  // 歪头：dir > 0 向画面右侧歪
  function triggerTilt(dir = 1) { tilt = 1; tiltDir = dir >= 0 ? 1 : -1; }
  // 转头看向某侧：dir -1..1（-1 画面左、+1 画面右），眼睛先到、头跟上
  function turnTo(dir, pitch = 0) {
    const d = clamp1(dir);
    lookAt(d, pitch, 1900);
    setPose(-d * 0.42, pitch * 0.25, d * 0.06, 1900);
  }
  function lookUp()   { lookAt(0,  0.8, 1600); setPose(0, -0.22, 0, 1600); }
  function lookDown() { lookAt(0, -0.8, 1600); setPose(0,  0.20, 0, 1600); }
  // 环视一圈：左 -> 右 -> 回中
  function lookAround() {
    turnTo(-0.9); 
    setTimeout(() => turnTo(0.9), 700);
    setTimeout(() => { lookAt(0, 0, 300); setPose(0, 0, 0, 300); }, 1500);
  }
  function resetPose() { lookAt(0, 0, 200); setPose(0, 0, 0, 200); }

  // ---------- 瞬时叠加动作（在当前表情之上短暂加一层）----------
  // 每项：{ t: 剩余进度 1->0, speed, peak, keys: [blendshape...] }
  const OVERLAYS = {
    '挑眉':   { speed: 1.6, peak: 0.75, keys: ['browOuterUpLeft','browOuterUpRight','browInnerUp'] },
    '单挑眉': { speed: 1.6, peak: 0.85, keys: ['browOuterUpLeft'] },
    '皱眉':   { speed: 1.2, peak: 0.8,  keys: ['browDownLeft','browDownRight'] },
    '鼓腮':   { speed: 0.9, peak: 0.85, keys: ['cheekPuff'] },
    '撅嘴':   { speed: 1.0, peak: 0.8,  keys: ['mouthPucker','mouthFunnel'] },
    '吐舌':   { speed: 1.1, peak: 0.7,  keys: ['tongueOut','jawOpen'] },
    '咬唇':   { speed: 1.0, peak: 0.7,  keys: ['mouthRollLower','mouthRollUpper','mouthPressLeft','mouthPressRight'] },
    '努嘴':   { speed: 1.1, peak: 0.6,  keys: ['mouthLeft','mouthPressRight'] },
  };
  const activeOverlays = new Map(); // name -> t
  function triggerOverlay(name) { if (OVERLAYS[name]) activeOverlays.set(name, 1); }
  function overlayNames() { return Object.keys(OVERLAYS); }

  // 主动眨眼（也用于"眨眼卖萌"的单眼眨）
  function triggerBlink(side = 'both') {
    if (side === 'left') { setBS('eyeBlinkLeft', 1); winkTimer = 0.22; winkSide = 'left'; }
    else if (side === 'right') { setBS('eyeBlinkRight', 1); winkTimer = 0.22; winkSide = 'right'; }
    else { blink = 1; nextBlink = performance.now() + 1800 + Math.random() * 2000; }
  }
  let winkTimer = 0, winkSide = null;

  // 眨眼
  let nextBlink = performance.now() + 1500, blink = 0;
  let nextSaccade = performance.now() + 2000;

  const clock = new THREE.Clock();
  function loop() {
    const dt = clock.getDelta();
    const now = performance.now();

    // 眨眼（困倦等表情会把眼皮压低，这里取较大值叠加，不互相抹掉）
    if (now > nextBlink) { blink = 1; nextBlink = now + 1800 + Math.random() * 2500; }
    blink = Math.max(0, blink - dt * 8);
    const lidBase = exprBase.eyeBlinkLeft || 0;
    if (winkTimer > 0) {
      // 单眼眨（wink）：只压一侧眼皮，另一侧照常
      winkTimer = Math.max(0, winkTimer - dt);
      const w = winkTimer > 0 ? 1 : 0;
      setBS('eyeBlinkLeft',  winkSide === 'left'  ? Math.max(lidBase, w) : Math.max(lidBase, blink));
      setBS('eyeBlinkRight', winkSide === 'right' ? Math.max(lidBase, w) : Math.max(lidBase, blink));
      if (winkTimer === 0) winkSide = null;
    } else {
      setBS('eyeBlinkLeft', Math.max(lidBase, blink));
      setBS('eyeBlinkRight', Math.max(lidBase, blink));
    }

    // 注视：外部没指定时做随机微扫视（saccade），这是"活着"感最廉价也最有效的来源
    gazeHold = Math.max(0, gazeHold - dt);
    if (gazeHold === 0 && now > nextSaccade) {
      gazeTX = (Math.random() * 2 - 1) * 0.35;
      gazeTY = (Math.random() * 2 - 1) * 0.22;
      nextSaccade = now + 1200 + Math.random() * 2600;
    }
    // 眼动是弹道式的：起跳快、落点稳，所以用比 blendshape 更高的逼近速度
    const gs = Math.min(1, 14 * dt);
    gazeX += (gazeTX - gazeX) * gs;
    gazeY += (gazeTY - gazeY) * gs;
    applyGaze();

    // 瞬时叠加动作：正弦包络（0 -> peak -> 0），结束后把这些 blendshape 还回表情基准
    for (const [name, t0] of [...activeOverlays]) {
      const conf = OVERLAYS[name];
      const t = t0 - dt * conf.speed;
      if (t <= 0) { activeOverlays.delete(name); restoreBS(conf.keys); continue; }
      activeOverlays.set(name, t);
      const k = Math.sin((1 - t) * Math.PI) * conf.peak;
      for (const key of conf.keys) addBS(key, k);
    }

    // 平滑逼近所有目标
    const speed = 12 * dt;
    const keys = new Set([...Object.keys(target), ...Object.keys(current)]);
    for (const k of keys) {
      ensure(k);
      const t = target[k] || 0;
      current[k] += (t - current[k]) * Math.min(1, speed);
      apply(k, current[k]);
    }

    // ---- 头颈姿态：持久姿态平滑逼近 + 瞬时动作叠加，最后按 6:4 分给颈骨/头骨 ----
    if (poseHold > 0) {
      poseHold = Math.max(0, poseHold - dt);
      if (poseHold === 0) { poseYaw = 0; posePitch = 0; poseRoll = 0; }
    }
    const ps = Math.min(1, 5 * dt);
    curYaw   += (poseYaw   - curYaw)   * ps;
    curPitch += (posePitch - curPitch) * ps;
    curRoll  += (poseRoll  - curRoll)  * ps;

    let dPitch = curPitch, dYaw = curYaw, dRoll = curRoll;
    if (nod > 0)   { nod   = Math.max(0, nod   - dt * 2.2); dPitch += Math.sin(nod * Math.PI) * 0.20; }
    if (shake > 0) { shake = Math.max(0, shake - dt * 2.2); dYaw   += Math.sin(shake * Math.PI * 2) * 0.22; }
    if (tilt > 0)  { tilt  = Math.max(0, tilt  - dt * 1.4); dRoll  += Math.sin(tilt * Math.PI) * 0.26 * tiltDir; }

    // 无所事事时的微幅摆动（idle sway），幅度小到不易察觉但能去掉"雕像感"
    const sway = Math.sin(now * 0.00042) * 0.012 + Math.sin(now * 0.00097) * 0.006;
    dYaw += sway; dRoll += sway * 0.5;

    setBoneRot(neckBone, dPitch * NECK_SHARE, dYaw * NECK_SHARE, dRoll * NECK_SHARE);
    setBoneRot(headBone, dPitch * HEAD_SHARE, dYaw * HEAD_SHARE, dRoll * HEAD_SHARE);

    // 呼吸：胸腔轻微起伏（约 15 次/分）
    if (spineBone) setBoneRot(spineBone, Math.sin(now * 0.0016) * 0.010, 0, 0);

    renderer.render(scene, cam);
    requestAnimationFrame(loop);
  }
  resize();
  mouthClosed();
  requestAnimationFrame(loop);

  return {
    setViseme, mouthClosed,
    setExpression, expressionNames,
    triggerNod, triggerShake, triggerTilt,
    lookAt, turnTo, lookUp, lookDown, lookAround, resetPose,
    triggerOverlay, overlayNames, triggerBlink,
    setListening,
  };
}



