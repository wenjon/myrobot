// Three.js 2D 虚拟头：脸、眼、眉、嘴。暴露 setMouth / setExpression / update。
// 使用全局 THREE（src/three.min.js）。
export function createHead(canvas) {
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x0f1220);
  const aspect = canvas.clientWidth / canvas.clientHeight;
  const cam = new THREE.OrthographicCamera(-aspect, aspect, 1, -1, 0.1, 10);
  cam.position.z = 2;
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
  renderer.setPixelRatio(window.devicePixelRatio || 1);
  function resize() {
    const w = canvas.clientWidth, h = canvas.clientHeight;
    renderer.setSize(w, h, false);
    const a = w / h;
    cam.left = -a; cam.right = a; cam.top = 1; cam.bottom = -1;
    cam.updateProjectionMatrix();
  }
  window.addEventListener('resize', resize);

  // 脸
  const face = new THREE.Mesh(
    new THREE.CircleGeometry(0.72, 64),
    new THREE.MeshBasicMaterial({ color: 0xffe0b2 })
  );
  scene.add(face);

  // 腮红
  function cheek(x) {
    const m = new THREE.Mesh(new THREE.CircleGeometry(0.1, 32),
      new THREE.MeshBasicMaterial({ color: 0xffb3a7, transparent: true, opacity: 0.6 }));
    m.position.set(x, -0.12, 0.01); scene.add(m); return m;
  }
  cheek(-0.42); cheek(0.42);

  // 眉毛
  function brow(x) {
    const g = new THREE.Mesh(new THREE.PlaneGeometry(0.22, 0.04),
      new THREE.MeshBasicMaterial({ color: 0x6d4c41 }));
    g.position.set(x, 0.34, 0.02); scene.add(g); return g;
  }
  const browL = brow(-0.26), browR = brow(0.26);

  // 眼睛（用缩放模拟眨眼）
  function eye(x) {
    const g = new THREE.Mesh(new THREE.CircleGeometry(0.09, 32),
      new THREE.MeshBasicMaterial({ color: 0x3e2723 }));
    g.position.set(x, 0.16, 0.02); scene.add(g); return g;
  }
  const eyeL = eye(-0.26), eyeR = eye(0.26);

  // 嘴：用一个可缩放的 mesh（宽度=张合，颜色深）
  const mouth = new THREE.Mesh(
    new THREE.CircleGeometry(0.16, 48),
    new THREE.MeshBasicMaterial({ color: 0x7a1f1f })
  );
  mouth.position.set(0, -0.34, 0.02);
  scene.add(mouth);

  const state = {
    mouthOpen: 0,      // 0..1 目标张口
    mouthWide: 0.5,    // 0..1 嘴型横向（圆->扁）
    curMouthOpen: 0,
    curMouthWide: 0.5,
    expression: '平静',
    browY: 0.34,
    targetBrowY: 0.34,
    blink: 0,
    nextBlink: performance.now() + 2000,
    nod: 0,            // 点头进度
  };

  const EXPR = {
    '平静': { brow: 0.34, browRot: 0, mouthColor: 0x7a1f1f },
    '开心': { brow: 0.38, browRot: 0.12, mouthColor: 0x9c2a2a },
    '疑惑': { brow: 0.40, browRot: 0.30, mouthColor: 0x6a2020 },
    '惊讶': { brow: 0.44, browRot: 0, mouthColor: 0x8a1f1f },
  };

  function setMouth(open, wide) {
    state.mouthOpen = Math.max(0, Math.min(1, open));
    if (wide !== undefined) state.mouthWide = Math.max(0, Math.min(1, wide));
  }
  function setExpression(name) {
    if (EXPR[name]) { state.expression = name; state.targetBrowY = EXPR[name].brow; }
  }
  function triggerNod() { state.nod = 1; }

  function lerp(a, b, t) { return a + (b - a) * t; }

  function update() {
    const now = performance.now();
    // 眨眼
    if (now > state.nextBlink) { state.blink = 1; state.nextBlink = now + 2000 + Math.random() * 2500; }
    state.blink = Math.max(0, state.blink - 0.15);
    const eyeScaleY = 1 - state.blink;
    eyeL.scale.y = eyeScaleY; eyeR.scale.y = eyeScaleY;

    // 嘴型平滑
    state.curMouthOpen = lerp(state.curMouthOpen, state.mouthOpen, 0.4);
    state.curMouthWide = lerp(state.curMouthWide, state.mouthWide, 0.3);
    const openH = 0.03 + state.curMouthOpen * 0.34;      // 纵向
    const wideW = 0.10 + state.curMouthWide * 0.30;      // 横向
    mouth.scale.set(wideW / 0.16, openH / 0.16, 1);
    const expr = EXPR[state.expression] || EXPR['平静'];
    mouth.material.color.setHex(expr.mouthColor);

    // 眉毛
    state.browY = lerp(state.browY, state.targetBrowY, 0.2);
    browL.position.y = state.browY; browR.position.y = state.browY;
    browL.rotation.z = expr.browRot; browR.rotation.z = -expr.browRot;

    // 点头
    if (state.nod > 0) {
      state.nod = Math.max(0, state.nod - 0.04);
      face.position.y = -Math.sin(state.nod * Math.PI) * 0.06;
    }
    const grp = [face, eyeL, eyeR, browL, browR, mouth];
    // 让所有随脸点头一起上下（简单整体位移）
    const ny = face.position.y;
    // eyes/brows/mouth 已相对脸中心，简单加偏移
    // （此处仅脸位移即可给出点头感）

    renderer.render(scene, cam);
    requestAnimationFrame(update);
  }

  resize();
  requestAnimationFrame(update);
  return { setMouth, setExpression, triggerNod };
}
