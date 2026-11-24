/* ===================================================================
   The hero scene: the hash chain.

   Every step of a run is written to the trace as one row, and each row
   carries the digest of the row before it, so the last digest stands
   for the whole run. That is the one idea in this project worth a
   picture, so the picture is literally that: plates linked front to
   back, each one locked to the last, receding into the dark.

   It is decoration with a referent, and it is cheap: one instanced
   wireframe, one line, one point cloud, no post-processing, no
   shadows. It never runs when the tab is hidden, when the visitor
   asks for reduced motion, or when the device has no WebGL.
   =================================================================== */

import * as THREE from 'https://cdn.jsdelivr.net/npm/three@0.169.0/build/three.module.js';

const PLATES = 30;          // links in the chain
const GAP = 1.5;            // distance between links, world units
const DUST = 700;

export function startScene(canvas) {
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return null;

  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({
      canvas, antialias: true, alpha: true, powerPreference: 'high-performance',
    });
  } catch {
    return null;                       // no WebGL: the page is fine without it
  }
  if (!renderer.getContext()) return null;

  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setSize(window.innerWidth, window.innerHeight, false);

  const scene = new THREE.Scene();
  scene.fog = new THREE.FogExp2(0x0e0e0f, 0.055);

  const camera = new THREE.PerspectiveCamera(42, window.innerWidth / window.innerHeight, 0.1, 160);
  camera.position.set(0, 0.35, 7);

  // Monochrome, like the rest of the page. The only colour in the
  // scene is amber on every sixth link, which is the approval gate:
  // the one place in the flow where the agent stops for a person.
  const paper = new THREE.Color(0xf4f3f1);
  const grey = new THREE.Color(0x9d9c99);
  const amber = new THREE.Color(0xf5b544);

  const chain = new THREE.Group();
  scene.add(chain);

  /* --- the links ---------------------------------------------------
     A plate is a rectangle outline (the document) with a fainter inner
     rectangle (what was read back). The pair is drawn once and reused
     by instancing the same geometry PLATES times down the z axis. */
  const outline = new THREE.EdgesGeometry(new THREE.BoxGeometry(2.1, 1.3, 0.04));
  const inner = new THREE.EdgesGeometry(new THREE.BoxGeometry(1.74, 1.0, 0.04));

  const plateMat = new THREE.LineBasicMaterial({ color: paper, transparent: true, opacity: 0.4 });
  const innerMat = new THREE.LineBasicMaterial({ color: grey, transparent: true, opacity: 0.15 });
  const gateMat = new THREE.LineBasicMaterial({ color: amber, transparent: true, opacity: 0.6 });

  const links = [];
  for (let i = 0; i < PLATES; i++) {
    const g = new THREE.Group();
    // Node 6 of 11 is the approval gate; mark every sixth link so the
    // chain reads as runs one after another, each with its own gate.
    const isGate = i % 6 === 5;
    g.add(new THREE.LineSegments(outline, isGate ? gateMat : plateMat));
    g.add(new THREE.LineSegments(inner, innerMat));
    g.position.z = -i * GAP;
    g.rotation.z = i * 0.055;             // the slow twist down the chain
    g.userData.phase = i * 0.42;
    chain.add(g);
    links.push(g);
  }

  /* --- the thread that links them --------------------------------- */
  const threadPts = links.map((l) => new THREE.Vector3(0, 0, l.position.z));
  const thread = new THREE.Line(
    new THREE.BufferGeometry().setFromPoints(threadPts),
    new THREE.LineBasicMaterial({ color: paper, transparent: true, opacity: 0.18 }),
  );
  chain.add(thread);

  /* --- dust ------------------------------------------------------- */
  const dustPos = new Float32Array(DUST * 3);
  for (let i = 0; i < DUST; i++) {
    dustPos[i * 3] = (Math.random() - 0.5) * 26;
    dustPos[i * 3 + 1] = (Math.random() - 0.5) * 16;
    dustPos[i * 3 + 2] = -Math.random() * PLATES * GAP;
  }
  const dustGeo = new THREE.BufferGeometry();
  dustGeo.setAttribute('position', new THREE.BufferAttribute(dustPos, 3));
  const dust = new THREE.Points(dustGeo, new THREE.PointsMaterial({
    color: 0x9d9c99, size: 0.02, transparent: true, opacity: 0.4, sizeAttenuation: true,
  }));
  scene.add(dust);

  /* --- input ------------------------------------------------------ */
  const pointer = { x: 0, y: 0, tx: 0, ty: 0 };
  const onPointer = (e) => {
    pointer.tx = (e.clientX / window.innerWidth - 0.5) * 2;
    pointer.ty = (e.clientY / window.innerHeight - 0.5) * 2;
  };
  window.addEventListener('pointermove', onPointer, { passive: true });

  let scroll = 0;            // 0..1 over the first screen and a half
  const onScroll = () => {
    const span = window.innerHeight * 1.6;
    scroll = Math.min(1, (window.scrollY || 0) / span);
    // The chain belongs to the hero. Past it the page is a record to
    // be read, and a wireframe drifting behind a table of money is
    // just something in the way, so it fades out and stops drawing.
    const fade = Math.max(0, 1 - scroll * 1.45);
    canvas.style.opacity = String(fade);
    canvas.style.visibility = fade < 0.02 ? 'hidden' : 'visible';
  };
  window.addEventListener('scroll', onScroll, { passive: true });
  onScroll();

  const onResize = () => {
    camera.aspect = window.innerWidth / window.innerHeight;
    camera.updateProjectionMatrix();
    renderer.setSize(window.innerWidth, window.innerHeight, false);
  };
  window.addEventListener('resize', onResize);

  /* --- the loop ---------------------------------------------------- */
  let raf = 0;
  let t = 0;
  const clock = new THREE.Clock();

  function frame() {
    raf = requestAnimationFrame(frame);
    const dt = Math.min(clock.getDelta(), 0.05);
    t += dt;

    if (canvas.style.visibility === 'hidden') return;   // off-screen: no work

    pointer.x += (pointer.tx - pointer.x) * 0.045;
    pointer.y += (pointer.ty - pointer.y) * 0.045;

    // The chain drifts forward on its own and is pushed further by the
    // scroll, so the first screen reads as travelling down the trace.
    chain.position.z = 2.4 + scroll * 7 + Math.sin(t * 0.22) * 0.25;
    chain.rotation.y = pointer.x * 0.16 + Math.sin(t * 0.14) * 0.045;
    chain.rotation.x = -pointer.y * 0.1;

    for (const l of links) {
      // Each link breathes a little out of phase: the page is alive but
      // nothing on it is urgent.
      const b = 1 + Math.sin(t * 0.8 + l.userData.phase) * 0.012;
      l.scale.set(b, b, 1);
    }

    dust.rotation.y = t * 0.012;
    dust.position.z = 2 + scroll * 10;

    camera.position.x += (pointer.x * 0.5 - camera.position.x) * 0.04;
    camera.position.y += (0.35 - pointer.y * 0.3 - camera.position.y) * 0.04;
    camera.lookAt(0, 0, -6);

    renderer.render(scene, camera);
  }

  const onVisibility = () => {
    if (document.hidden) { cancelAnimationFrame(raf); raf = 0; }
    else if (!raf) { clock.getDelta(); frame(); }
  };
  document.addEventListener('visibilitychange', onVisibility);

  frame();
  canvas.classList.add('ready');

  return {
    stop() {
      cancelAnimationFrame(raf);
      window.removeEventListener('pointermove', onPointer);
      window.removeEventListener('scroll', onScroll);
      window.removeEventListener('resize', onResize);
      document.removeEventListener('visibilitychange', onVisibility);
      renderer.dispose();
    },
  };
}
