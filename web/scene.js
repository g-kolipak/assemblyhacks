// BLINK's mascot: a 3D eye that listens, scans the web while a claim is being
// checked, blinks on every verdict and reacts to it (nod, head shake, wobble).
import * as THREE from "three";

const COLOR = {
  checking: 0x8b5cf6,
  lid: 0x808080,
  true: 0x2bd38a,
  false: 0xff4d6d,
  misleading: 0xffc53d,
  unverified: 0xb9b4ff,
};

const EYE_VERT = /* glsl */ `
  varying vec3 vLocal;
  varying vec3 vNormalW;
  varying vec3 vViewDir;
  void main() {
    vLocal = position;
    vec4 wp = modelMatrix * vec4(position, 1.0);
    vNormalW = normalize(mat3(modelMatrix) * normal);
    vViewDir = normalize(cameraPosition - wp.xyz);
    gl_Position = projectionMatrix * viewMatrix * wp;
  }
`;

// The logo's eye: white ball, big black iris, two white catchlights. Painted
// by angle from the forward axis, so turning the ball moves the iris too.
// uTint glows around the iris rim while checking and on each verdict.
const EYE_FRAG = /* glsl */ `
  uniform vec3 uTint;
  uniform float uTintAmt;
  uniform float uIrisR;
  varying vec3 vLocal;
  varying vec3 vNormalW;
  varying vec3 vViewDir;
  void main() {
    vec3 p = normalize(vLocal);
    float ang = acos(clamp(p.z, -1.0, 1.0));
    float disc = 1.0 - smoothstep(uIrisR - 0.008, uIrisR + 0.008, ang);
    float band = smoothstep(uIrisR - 0.16, uIrisR - 0.015, ang);
    vec3 iris = mix(vec3(0.0), uTint, band * uTintAmt);
    vec3 col = mix(vec3(1.0), iris, disc);
    float s = sin(uIrisR) / 0.48;
    float c1 = 1.0 - smoothstep(0.11, 0.12, length(p.xy - vec2(0.3, 0.3) * s) / s);
    float c2 = 1.0 - smoothstep(0.052, 0.062, length(p.xy - vec2(0.17, 0.066) * s) / s);
    col = mix(col, vec3(1.0), max(c1, c2) * disc);
    vec3 L = normalize(vec3(0.45, 0.8, 1.0));
    float diff = 0.78 + 0.22 * max(dot(vNormalW, L), 0.0);
    vec3 H = normalize(L + vViewDir);
    float spec = pow(max(dot(vNormalW, H), 0.0), 140.0) * 0.6;
    col = col * diff + vec3(spec);
    gl_FragColor = vec4(col, 1.0);
    #include <colorspace_fragment>
  }
`;

const LIDS = {
  idle: [0.85, 0.62],
  listening: [1.0, 0.75],
  checking: [0.42, 0.48],
};

const rand = (a, b) => a + Math.random() * (b - a);
const damp = (cur, target, rate, dt) => cur + (target - cur) * (1 - Math.exp(-rate * dt));

export function createEye(canvas, anchor, feed) {
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(35, 1, 0.1, 100);
  camera.position.set(0, 0, 10);
  scene.add(new THREE.HemisphereLight(0xffffff, 0x333333, 1.7));
  const sun = new THREE.DirectionalLight(0xffffff, 2.4);
  sun.position.set(3, 5, 6);
  scene.add(sun);

  // rig: follows the #stage element. head: verdict reactions. ball: gaze.
  const rig = new THREE.Group();
  const head = new THREE.Group();
  const ball = new THREE.Group();
  rig.add(head);
  head.add(ball);
  scene.add(rig);

  const uniforms = {
    uTint: { value: new THREE.Color(COLOR.checking) },
    uTintAmt: { value: 0 },
    uIrisR: { value: 0.5 },
  };
  ball.add(new THREE.Mesh(
    new THREE.SphereGeometry(1, 96, 64),
    new THREE.ShaderMaterial({ uniforms, vertexShader: EYE_VERT, fragmentShader: EYE_FRAG }),
  ));

  // Eyelids: two hemispherical shells that swing open around the x axis.
  const lidMat = new THREE.MeshStandardMaterial({ color: COLOR.lid, roughness: 0.6, side: THREE.DoubleSide });
  const lidGeo = (start) => new THREE.SphereGeometry(1.06, 64, 24, 0, Math.PI * 2, start, Math.PI / 2);
  const rimGeo = new THREE.TorusGeometry(1.06, 0.045, 12, 96);
  const upper = new THREE.Mesh(lidGeo(0), lidMat);
  const lower = new THREE.Mesh(lidGeo(Math.PI / 2), lidMat);
  for (const lid of [upper, lower]) {
    const rim = new THREE.Mesh(rimGeo, lidMat);
    rim.rotation.x = Math.PI / 2;
    lid.add(rim);
    head.add(lid);
  }

  // Background confetti shapes, drifting slowly far behind the eye.
  const floaters = [];
  const shapes = [
    new THREE.IcosahedronGeometry(0.2, 0),
    new THREE.TorusGeometry(0.17, 0.065, 8, 24),
    new THREE.OctahedronGeometry(0.2),
  ];
  const palette = [COLOR.checking, COLOR.true, COLOR.false, COLOR.misleading, COLOR.unverified, 0xffffff];
  for (let i = 0; i < (reduce ? 0 : 18); i++) {
    const mesh = new THREE.Mesh(
      shapes[i % shapes.length],
      new THREE.MeshStandardMaterial({ color: palette[i % palette.length], roughness: 0.4, flatShading: true }),
    );
    mesh.position.set(rand(-9, 9), rand(-5.5, 5.5), rand(-7, -2.5));
    mesh.rotation.set(rand(0, 6), rand(0, 6), 0);
    floaters.push({ mesh, spin: rand(0.2, 0.7), bob: rand(0, 6), y: mesh.position.y });
    scene.add(mesh);
  }

  const effects = [];
  const dummy = new THREE.Object3D();

  function ring(color) {
    const mesh = new THREE.Mesh(
      new THREE.RingGeometry(1.12, 1.22, 96),
      new THREE.MeshBasicMaterial({ color, transparent: true, side: THREE.DoubleSide }),
    );
    rig.add(mesh);
    effects.push({ t: 0, life: 0.8, mesh, step(t) {
      mesh.scale.setScalar(1 + t * 2.2);
      mesh.material.opacity = 0.9 * (1 - t);
    } });
  }

  function confetti(color) {
    const n = 44;
    const mesh = new THREE.InstancedMesh(
      new THREE.TetrahedronGeometry(0.08),
      new THREE.MeshStandardMaterial({ color, roughness: 0.5, flatShading: true, transparent: true }),
      n,
    );
    const parts = Array.from({ length: n }, () => {
      const dir = new THREE.Vector3(rand(-1, 1), rand(-0.4, 1), rand(0.2, 1)).normalize();
      return { p: dir.clone().multiplyScalar(1.1), v: dir.multiplyScalar(rand(2.5, 5.5)), r: rand(0, 6), s: rand(0.6, 1.4) };
    });
    rig.add(mesh);
    let last = 0;
    effects.push({ t: 0, life: 1.5, mesh, step(t) {
      const dt = (t - last) * 1.5;
      last = t;
      parts.forEach((q, i) => {
        q.v.y -= 7 * dt;
        q.p.addScaledVector(q.v, dt);
        q.r += dt * 8;
        dummy.position.copy(q.p);
        dummy.rotation.set(q.r, q.r * 0.7, 0);
        dummy.scale.setScalar(q.s);
        dummy.updateMatrix();
        mesh.setMatrixAt(i, dummy.matrix);
      });
      mesh.instanceMatrix.needsUpdate = true;
      mesh.material.opacity = 1 - t * t;
    } });
  }

  // State the app drives.
  let listening = false;
  let checking = false;
  let level = 0;
  let reaction = { kind: null, t: 0 };
  let flash = 0;
  let blinkT = -1;
  let nextBlink = rand(2, 5);
  let lid = [...LIDS.idle];
  const gaze = { x: 0, y: 0, tx: 0, ty: 0, hold: 0, nextMove: 0 };
  const pointer = { x: 0, y: 0, at: -10 };
  const flashColor = new THREE.Color(COLOR.true);
  const checkColor = new THREE.Color(COLOR.checking);
  let screen = { cx: 0, cy: 0 };
  let time = 0;

  addEventListener("pointermove", (e) => {
    pointer.x = e.clientX;
    pointer.y = e.clientY;
    pointer.at = time;
  }, { passive: true });

  function place() {
    const r = anchor.getBoundingClientRect();
    const halfH = Math.tan(THREE.MathUtils.degToRad(camera.fov / 2)) * camera.position.z;
    const halfW = halfH * camera.aspect;
    const cx = r.left + r.width / 2;
    const cy = r.top + r.height / 2;
    rig.position.set((cx / innerWidth * 2 - 1) * halfW, -(cy / innerHeight * 2 - 1) * halfH, 0);
    rig.scale.setScalar((Math.min(r.width, r.height * 0.9) * 0.34) / innerHeight * 2 * halfH);
    screen = { cx, cy };
  }

  function resize() {
    renderer.setSize(innerWidth, innerHeight, false);
    camera.aspect = innerWidth / innerHeight;
    camera.updateProjectionMatrix();
  }
  addEventListener("resize", resize);
  resize();

  function lookAtScreen(x, y) {
    return {
      x: THREE.MathUtils.clamp((x - screen.cx) / (innerWidth * 0.4), -1, 1),
      y: THREE.MathUtils.clamp(-(y - screen.cy) / (innerHeight * 0.4), -1, 1),
    };
  }

  function updateGaze(dt) {
    if (time < gaze.hold) {
      // keep looking where a verdict told us to
    } else if (checking && !reduce) {
      if (time > gaze.nextMove) { // saccades: scanning the web
        gaze.tx = rand(-0.9, 0.9);
        gaze.ty = rand(-0.15, 0.35);
        gaze.nextMove = time + rand(0.12, 0.24);
      }
    } else if (time - pointer.at < 2.5) {
      const g = lookAtScreen(pointer.x, pointer.y);
      gaze.tx = g.x;
      gaze.ty = g.y;
    } else if (!reduce && time > gaze.nextMove) {
      gaze.tx = rand(-0.6, 0.6);
      gaze.ty = rand(-0.3, 0.4);
      gaze.nextMove = time + rand(1.2, 3);
    }
    const rate = checking ? 28 : 9;
    gaze.x = damp(gaze.x, gaze.tx, rate, dt);
    gaze.y = damp(gaze.y, gaze.ty, rate, dt);
    ball.rotation.y = gaze.x * 0.55;
    ball.rotation.x = -gaze.y * 0.42;
  }

  function updateLids(dt) {
    const base = checking ? LIDS.checking : listening ? LIDS.listening : LIDS.idle;
    lid[0] = damp(lid[0], base[0], 10, dt);
    lid[1] = damp(lid[1], base[1], 10, dt);
    let shut = 0;
    if (blinkT >= 0) {
      blinkT += dt;
      shut = Math.sin(Math.min(blinkT / 0.18, 1) * Math.PI);
      if (blinkT > 0.18) blinkT = -1;
    } else if (time > nextBlink) {
      blinkT = 0;
      nextBlink = time + rand(2.2, 6);
    }
    upper.rotation.x = -lid[0] * (1 - shut);
    lower.rotation.x = lid[1] * (1 - shut);
  }

  function updateReaction(dt) {
    reaction.t += dt;
    const k = reaction.t;
    const fade = Math.exp(-k * 2.8);
    head.position.set(0, 0, 0);
    head.rotation.set(0, 0, 0);
    if (reduce) return;
    switch (reaction.kind) {
      case "true": head.position.y = Math.abs(Math.sin(k * 11)) * 0.28 * fade; break;
      case "false": head.rotation.y = Math.sin(k * 24) * 0.38 * fade; break;
      case "misleading": head.rotation.z = Math.sin(k * 9) * 0.32 * fade; break;
      case "unverified": head.position.y = Math.sin(Math.min(k / 0.7, 1) * Math.PI) * 0.18; break;
      case "poke": head.scale.setScalar(1 + Math.sin(k * 18) * 0.12 * fade); return;
    }
    head.scale.setScalar(1);
  }

  const clock = new THREE.Clock();
  renderer.setAnimationLoop(() => {
    const dt = Math.min(clock.getDelta(), 0.05);
    time += dt;
    place();
    updateGaze(dt);
    updateLids(dt);
    updateReaction(dt);

    flash = Math.max(0, flash - dt * 0.6);
    const flashAmt = Math.min(1, flash * 1.6);
    uniforms.uTint.value.lerp(flashAmt > 0.05 ? flashColor : checkColor, 1 - Math.exp(-12 * dt));
    uniforms.uTintAmt.value = damp(uniforms.uTintAmt.value, Math.max(flashAmt, checking ? 0.9 : 0), 10, dt);
    const iris = checking ? 0.42 : 0.48 + Math.min(level * 3, 1) * 0.06;
    uniforms.uIrisR.value = damp(uniforms.uIrisR.value, iris, 14, dt);

    for (const f of floaters) {
      f.mesh.rotation.x += f.spin * dt;
      f.mesh.rotation.y += f.spin * 0.6 * dt;
      f.mesh.position.y = f.y + Math.sin(time * 0.5 + f.bob) * 0.25;
    }
    for (let i = effects.length - 1; i >= 0; i--) {
      const fx = effects[i];
      fx.t += dt;
      fx.step(Math.min(fx.t / fx.life, 1));
      if (fx.t >= fx.life) {
        rig.remove(fx.mesh);
        fx.mesh.geometry.dispose();
        fx.mesh.material.dispose();
        effects.splice(i, 1);
      }
    }
    renderer.render(scene, camera);
  });

  return {
    setListening(on) { listening = on; if (!on) level = 0; },
    setChecking(on) { checking = on; },
    setLevel(x) { level = damp(level, x, 0.5, 1); },
    blink() { blinkT = 0; },
    poke() { blinkT = 0; reaction = { kind: "poke", t: 0 }; },
    verdict(kind) {
      const color = COLOR[kind] ?? COLOR.unverified;
      blinkT = 0;
      reaction = { kind, t: 0 };
      flashColor.setHex(color);
      flash = 1.6;
      if (!reduce) {
        ring(color);
        confetti(color);
        const r = feed.getBoundingClientRect();
        const g = lookAtScreen(r.left + r.width / 2, Math.max(r.top + 40, 0));
        gaze.tx = g.x;
        gaze.ty = g.y;
        gaze.hold = time + 1.1;
      }
    },
  };
}
