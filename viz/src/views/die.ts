// 3D "glass" die: floorplan blocks, NoC mesh, transaction particles, NPU tiles, DRAM queues.
import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import { RoomEnvironment } from 'three/examples/jsm/environments/RoomEnvironment.js';
import { RoundedBoxGeometry } from 'three/examples/jsm/geometries/RoundedBoxGeometry.js';
import { CSS2DObject, CSS2DRenderer } from 'three/examples/jsm/renderers/CSS2DRenderer.js';
import { EffectComposer } from 'three/examples/jsm/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/examples/jsm/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/examples/jsm/postprocessing/UnrealBloomPass.js';
import { OutputPass } from 'three/examples/jsm/postprocessing/OutputPass.js';
import { ShaderPass } from 'three/examples/jsm/postprocessing/ShaderPass.js';
import { Spring } from '../clock';
import { taskColor, voltageTint } from '../palette';
import type { ReplayStore } from '../replay/store';
import { KIND, NONE16 } from '../replay/types';
import type { BlockInfo, CritOp } from '../replay/types';

const DIE = 10;
const Y_NOC = 1.15;
const MAX_PARTICLES = 262144;
const NICE: Record<string, string> = {
  cpu_a: 'CPU A', cpu_b: 'CPU B', rt: 'RT core', dsp: 'Vision DSP', npu: 'NPU', simt: 'SIMT', sys_cache: 'System cache',
  dram: 'DRAM', dma: 'DMA',
};
const UNIT_COUNT_KEY: Record<string, string> = { cpu: 'cores', rtcpu: 'cores', dsp: 'units', npu: 'tiles', simt: 'compute_units', dma: 'engines' };

export function blockLabel(id: string): string {
  return NICE[id] ?? id;
}

interface BlockObj {
  info: BlockInfo;
  index: number;
  mesh: THREE.Mesh;
  mat: THREE.MeshPhysicalMaterial;
  core: THREE.MeshBasicMaterial;
  edges: THREE.LineSegments;
  label: CSS2DObject;
  labelSub: HTMLElement;
  height: Spring;
  glow: Spring;
  units: number;
  domain: string;
  cx: number; cz: number; w: number; d: number;
}

export interface UnitActivity { task: number; progress: number; layer: number; job: number }

function nodeWorld(x: number, y: number, dims: number[]): THREE.Vector3 {
  return new THREE.Vector3(((x + 0.5) / dims[0] - 0.5) * DIE, Y_NOC, ((y + 0.5) / dims[1] - 0.5) * DIE);
}

const PARTICLE_VS = /* glsl */ `
  attribute vec2 aSrc;
  attribute vec2 aDst;
  attribute float aT0;
  attribute vec3 aColor;
  attribute float aSize;
  uniform float uTime;
  uniform float uTravel;
  uniform float uSpacing;
  uniform vec2 uDims;
  uniform float uY;
  uniform float uOpacity;
  uniform float uGain;
  uniform float uTrail;
  varying vec2 vUv;
  varying vec3 vColor;
  varying float vAlpha;
  vec3 nodePos(vec2 n) {
    return vec3((n.x + 0.5) / uDims.x - 0.5, 0.0, (n.y + 0.5) / uDims.y - 0.5) * vec3(${DIE.toFixed(1)}, 1.0, ${DIE.toFixed(1)});
  }
  void main() {
    vec2 d = aDst - aSrc;
    float lx = abs(d.x);
    float ly = abs(d.y);
    float L = max(lx + ly, 0.5);
    float T = uTravel * (0.55 + 0.15 * L);
    float p = (uTime - aT0) / T;
    if (p < 0.0 || p > 1.12) { gl_Position = vec4(2.0, 2.0, 2.0, 1.0); vAlpha = 0.0; return; }
    float s = clamp(p, 0.0, 1.0) * L;
    vec2 n; vec2 dir; float segPos;
    if (lx + ly < 0.01) { n = aSrc; dir = vec2(1.0, 0.0); segPos = 0.0; }
    else if (s < lx) { n = aSrc + vec2(sign(d.x) * s, 0.0); dir = vec2(sign(d.x), 0.0); segPos = s; }
    else { n = vec2(aDst.x, aSrc.y + sign(d.y) * (s - lx)); dir = vec2(0.0, sign(d.y)); segPos = s - lx; }
    vec3 head = nodePos(n);
    vec3 fwd = vec3(dir.x, 0.0, dir.y);
    vec3 side = vec3(-dir.y, 0.0, dir.x);
    float trail = min(segPos, 0.6) * uSpacing * uTrail;
    vec3 pos = head + fwd * (position.x * trail) + side * (position.y * aSize);
    pos.y = uY + 0.04;
    vUv = vec2(position.x + 1.0, position.y + 0.5);
    vColor = aColor;
    vAlpha = (1.0 - smoothstep(1.0, 1.12, p)) * uOpacity * uGain;
    gl_Position = projectionMatrix * modelViewMatrix * vec4(pos, 1.0);
  }
`;
const PARTICLE_FS = /* glsl */ `
  varying vec2 vUv;
  varying vec3 vColor;
  varying float vAlpha;
  void main() {
    float along = clamp(vUv.x, 0.0, 1.0);
    float across = clamp(abs(vUv.y - 0.5) * 2.0, 0.0, 1.0);
    float a = pow(along, 2.2) * (1.0 - across * across);
    float head = smoothstep(0.82, 1.0, along) * (1.0 - across);
    vec3 c = vColor * (0.7 + 1.8 * head);
    gl_FragColor = vec4(max(c * a * vAlpha, vec3(0.0)), clamp(a * vAlpha, 0.0, 1.0));
  }
`;

export class DieView {
  readonly renderer: THREE.WebGLRenderer;
  readonly scene = new THREE.Scene();
  readonly camera: THREE.PerspectiveCamera;
  readonly controls: OrbitControls;
  private composer: EffectComposer;
  private bloom: UnrealBloomPass;
  private labels: CSS2DRenderer;
  private blocks: BlockObj[] = [];
  private blockById = new Map<string, BlockObj>();
  private store: ReplayStore | null = null;
  private dims = [4, 4];
  private linkGlow: { mesh: THREE.Mesh; mat: THREE.MeshBasicMaterial; util: Spring; a: number; b: number }[] = [];
  private linkIndex = new Map<string, number>();
  private routers: THREE.Mesh[] = [];
  private particles!: THREE.Mesh;
  private pGeo!: THREE.InstancedBufferGeometry;
  private pMat!: THREE.ShaderMaterial;
  private aSrc!: THREE.InstancedBufferAttribute;
  private aDst!: THREE.InstancedBufferAttribute;
  private aT0!: THREE.InstancedBufferAttribute;
  private aColor!: THREE.InstancedBufferAttribute;
  private aSize!: THREE.InstancedBufferAttribute;
  private window = { base: 0, t0: -1, t1: -1, travel: 0, complete: false };
  particleCount = 0;
  private npuCells: THREE.InstancedMesh | null = null;
  private npuCellInfo: { tile: number; i: number; j: number }[] = [];
  private dramCols: { mesh: THREE.Mesh; mat: THREE.MeshStandardMaterial; h: Spring; label: HTMLElement; ctrl: number }[] = [];
  private taskColors: THREE.Color[] = [];
  private pathGroup = new THREE.Group();
  private pathAnim: { mesh: THREE.Mesh; total: number; t: number; dur: number; hold: number }[] = [];
  private pulse: { block: BlockObj; t: number } | null = null;
  heatOverlay = false;
  private shift = new Spring(0, 140, 24);
  private size = { w: 1, h: 1 };
  showLabels = true;
  unitActivity = new Map<string, UnitActivity>();
  private tmpColor = new THREE.Color();
  private raycaster = new THREE.Raycaster();
  onHover: ((block: string | null, x: number, y: number) => void) | null = null;

  constructor(private canvas: HTMLCanvasElement, private labelEl: HTMLElement) {
    this.renderer = new THREE.WebGLRenderer({ canvas, antialias: true, powerPreference: 'high-performance' });
    this.renderer.setPixelRatio(Math.min(devicePixelRatio, 1.5));
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.0;
    const pmrem = new THREE.PMREMGenerator(this.renderer);
    this.scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.03).texture;
    this.scene.environmentIntensity = 0.32;
    this.scene.background = new THREE.Color('#0a0d13');

    this.camera = new THREE.PerspectiveCamera(30, 1, 0.1, 200);
    this.camera.position.set(0.5, 16.4, 14.0);
    this.controls = new OrbitControls(this.camera, canvas);
    this.controls.target.set(0, 0.2, 0.4);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.07;
    this.controls.minDistance = 6;
    this.controls.maxDistance = 45;
    this.controls.maxPolarAngle = Math.PI * 0.46;
    this.controls.update();

    this.scene.add(new THREE.HemisphereLight('#cfe0ff', '#0b0e14', 0.28));
    const key = new THREE.DirectionalLight('#ffffff', 0.9);
    key.position.set(-6, 14, 8);
    this.scene.add(key);
    const fill = new THREE.PointLight('#6f8cff', 12, 30, 2);
    fill.position.set(8, 4, -6);
    this.scene.add(fill);

    // multisampled target so edges stay clean at a capped pixel ratio
    const rt = new THREE.WebGLRenderTarget(1, 1, { type: THREE.HalfFloatType, samples: 4 });
    this.composer = new EffectComposer(this.renderer, rt);
    this.composer.addPass(new RenderPass(this.scene, this.camera));
    // additive particles can overflow half-float targets; clamp before bloom so Inf never becomes NaN
    this.composer.addPass(new ShaderPass({
      uniforms: { tDiffuse: { value: null } },
      vertexShader: 'varying vec2 vUv; void main(){ vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }',
      fragmentShader: 'uniform sampler2D tDiffuse; varying vec2 vUv; void main(){ vec4 c = texture2D(tDiffuse, vUv); c.rgb = clamp(c.rgb, 0.0, 24.0); if (any(isnan(c))) c = vec4(0.0); gl_FragColor = c; }',
    }));
    this.bloom = new UnrealBloomPass(new THREE.Vector2(512, 512), 0.62, 0.42, 0.88);
    this.composer.addPass(this.bloom);
    this.composer.addPass(new OutputPass());

    this.labels = new CSS2DRenderer({ element: labelEl });
    this.scene.add(this.pathGroup);
    canvas.addEventListener('pointermove', (e) => this.hover(e));
    canvas.addEventListener('pointerleave', () => this.onHover?.(null, 0, 0));
  }

  private hover(e: PointerEvent): void {
    const r = this.canvas.getBoundingClientRect();
    const ndc = new THREE.Vector2(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
    this.raycaster.setFromCamera(ndc, this.camera);
    const hit = this.raycaster.intersectObjects(this.blocks.map((b) => b.mesh), false)[0];
    const b = hit ? this.blocks.find((x) => x.mesh === hit.object) : undefined;
    this.onHover?.(b ? b.info.id : null, e.clientX - r.left, e.clientY - r.top);
  }

  setStore(store: ReplayStore): void {
    const firstBuild = this.store === null || JSON.stringify(this.store.manifest.soc) !== JSON.stringify(store.manifest.soc);
    this.store = store;
    this.taskColors = store.manifest.tasks.map((t) => new THREE.Color(taskColor(t.id, t.color)));
    this.window = { base: 0, t0: -1, t1: -1, travel: 0, complete: false };
    if (firstBuild) this.build();
  }

  private clearScene(): void {
    for (const b of this.blocks) { b.mesh.removeFromParent(); b.label.removeFromParent(); }
    this.blocks = [];
    this.blockById.clear();
  }

  private build(): void {
    const m = this.store!.manifest;
    this.clearScene();
    this.dims = m.soc.noc.dims ?? [4, 4];
    // substrate
    const sub = new THREE.Mesh(
      new RoundedBoxGeometry(DIE + 0.7, 0.32, DIE + 0.7, 4, 0.12),
      new THREE.MeshPhysicalMaterial({ color: '#10151f', roughness: 0.35, metalness: 0.5, clearcoat: 1, clearcoatRoughness: 0.2 }),
    );
    sub.position.y = -0.16;
    this.scene.add(sub);
    const grid = new THREE.GridHelper(DIE, 40, '#1f2a3a', '#161e2a');
    grid.position.y = 0.002;
    (grid.material as THREE.Material).transparent = true;
    (grid.material as THREE.Material).opacity = 0.55;
    this.scene.add(grid);
    const pkg = new THREE.Mesh(
      new RoundedBoxGeometry(DIE + 3.2, 0.25, DIE + 3.2, 4, 0.3),
      new THREE.MeshStandardMaterial({ color: '#0d1119', roughness: 0.8, metalness: 0.2 }),
    );
    pkg.position.y = -0.45;
    this.scene.add(pkg);

    m.soc.blocks.forEach((info, index) => {
      const fp = info.floorplan;
      const w = fp.w * DIE - 0.14;
      const d = fp.h * DIE - 0.14;
      const geo = new RoundedBoxGeometry(w, 1, d, 3, 0.07);
      geo.translate(0, 0.5, 0);
      const mat = new THREE.MeshPhysicalMaterial({
        color: '#8fa6c8', roughness: 0.06, metalness: 0.0, transparent: true, opacity: 0.42,
        clearcoat: 1, clearcoatRoughness: 0.06, emissive: new THREE.Color('#000000'), emissiveIntensity: 1,
        ior: 1.5, specularIntensity: 1, depthWrite: false,
      });
      const mesh = new THREE.Mesh(geo, mat);
      const coreGeo = new RoundedBoxGeometry(Math.max(0.2, w - 0.34), 0.8, Math.max(0.2, d - 0.34), 2, 0.05);
      coreGeo.translate(0, 0.42, 0);
      const coreMat = new THREE.MeshBasicMaterial({ color: '#334', transparent: true, opacity: 0.9, depthWrite: true });
      const core = new THREE.Mesh(coreGeo, coreMat);
      core.renderOrder = 0;
      mesh.add(core);
      const cx = (fp.x + fp.w / 2 - 0.5) * DIE;
      const cz = (fp.y + fp.h / 2 - 0.5) * DIE;
      mesh.position.set(cx, 0, cz);
      mesh.scale.y = 0.18;
      mesh.renderOrder = 1;
      this.scene.add(mesh);
      const edges = new THREE.LineSegments(
        new THREE.EdgesGeometry(geo, 30),
        new THREE.LineBasicMaterial({ color: '#d6e4ff', transparent: true, opacity: 0.22 }),
      );
      mesh.add(edges);
      const el = document.createElement('div');
      el.className = 'die-label';
      const name = document.createElement('div');
      name.className = 'die-label-name';
      name.textContent = blockLabel(info.id);
      const sub2 = document.createElement('div');
      sub2.className = 'die-label-sub';
      el.append(name, sub2);
      const label = new CSS2DObject(el);
      label.position.set(cx - w / 2 + 0.25, 0.05, cz - d / 2 + 0.3);
      label.center.set(0, 0);
      this.scene.add(label);
      const units = Number(info[UNIT_COUNT_KEY[info.kind] ?? ''] ?? 1) || 1;
      const b: BlockObj = {
        info, index, mesh, mat, core: coreMat, edges, label, labelSub: sub2, height: new Spring(0.18, 120, 22), glow: new Spring(0, 90, 18),
        units, domain: String(info.power_domain ?? ''), cx, cz, w, d,
      };
      this.blocks.push(b);
      this.blockById.set(info.id, b);
    });

    // NoC routers and links
    const [nx, ny] = this.dims;
    const routerGeo = new RoundedBoxGeometry(0.34, 0.12, 0.34, 2, 0.04);
    const routerMat = new THREE.MeshPhysicalMaterial({ color: '#4a5d7e', roughness: 0.3, metalness: 0.4, emissive: '#22365a', emissiveIntensity: 0.5 });
    const viaMat = new THREE.LineBasicMaterial({ color: '#6e86ad', transparent: true, opacity: 0.28 });
    for (let y = 0; y < ny; y++) {
      for (let x = 0; x < nx; x++) {
        const p = nodeWorld(x, y, this.dims);
        const r = new THREE.Mesh(routerGeo, routerMat);
        r.position.copy(p);
        this.scene.add(r);
        this.routers.push(r);
        const via = new THREE.BufferGeometry().setFromPoints([p.clone(), new THREE.Vector3(p.x, 0.15, p.z)]);
        this.scene.add(new THREE.Line(via, viaMat));
      }
    }
    const tubeMat = new THREE.MeshPhysicalMaterial({ color: '#9db6dd', roughness: 0.1, metalness: 0, transparent: true, opacity: 0.16, depthWrite: false });
    m.lod.links.forEach(([a, b], li) => {
      this.linkIndex.set(`${a}-${b}`, li);
      if (a > b) return; // one tube per undirected pair
      const pa = nodeWorld(a % nx, Math.floor(a / nx), this.dims);
      const pb = nodeWorld(b % nx, Math.floor(b / nx), this.dims);
      const len = pa.distanceTo(pb);
      const tube = new THREE.Mesh(new THREE.CylinderGeometry(0.07, 0.07, len, 16, 1, true), tubeMat);
      tube.position.copy(pa).add(pb).multiplyScalar(0.5);
      tube.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), pb.clone().sub(pa).normalize());
      this.scene.add(tube);
      const gmat = new THREE.MeshBasicMaterial({ color: '#7fb0ff', transparent: true, opacity: 0, blending: THREE.AdditiveBlending, depthWrite: false });
      const glow = new THREE.Mesh(new THREE.CylinderGeometry(0.05, 0.05, len, 12, 1, true), gmat);
      glow.position.copy(tube.position);
      glow.quaternion.copy(tube.quaternion);
      this.scene.add(glow);
      this.linkGlow.push({ mesh: glow, mat: gmat, util: new Spring(0, 60, 16), a, b });
    });

    this.buildParticles();
    this.buildNpu();
    this.buildDram();
  }

  private buildParticles(): void {
    const quad = new THREE.PlaneGeometry(1, 1);
    quad.translate(-0.5, 0, 0); // x in [-1, 0], y in [-0.5, 0.5]
    const g = new THREE.InstancedBufferGeometry();
    g.index = quad.index;
    g.setAttribute('position', quad.getAttribute('position'));
    this.aSrc = new THREE.InstancedBufferAttribute(new Float32Array(MAX_PARTICLES * 2), 2);
    this.aDst = new THREE.InstancedBufferAttribute(new Float32Array(MAX_PARTICLES * 2), 2);
    this.aT0 = new THREE.InstancedBufferAttribute(new Float32Array(MAX_PARTICLES), 1);
    this.aColor = new THREE.InstancedBufferAttribute(new Float32Array(MAX_PARTICLES * 3), 3);
    this.aSize = new THREE.InstancedBufferAttribute(new Float32Array(MAX_PARTICLES), 1);
    for (const a of [this.aSrc, this.aDst, this.aT0, this.aColor, this.aSize]) a.setUsage(THREE.DynamicDrawUsage);
    g.setAttribute('aSrc', this.aSrc);
    g.setAttribute('aDst', this.aDst);
    g.setAttribute('aT0', this.aT0);
    g.setAttribute('aColor', this.aColor);
    g.setAttribute('aSize', this.aSize);
    g.instanceCount = 0;
    this.pGeo = g;
    this.pMat = new THREE.ShaderMaterial({
      vertexShader: PARTICLE_VS, fragmentShader: PARTICLE_FS, transparent: true, depthWrite: false, side: THREE.DoubleSide,
      blending: THREE.AdditiveBlending,
      uniforms: {
        uTime: { value: 0 }, uTravel: { value: 1 }, uSpacing: { value: DIE / this.dims[0] },
        uDims: { value: new THREE.Vector2(this.dims[0], this.dims[1]) }, uY: { value: Y_NOC }, uOpacity: { value: 1 }, uGain: { value: 1 }, uTrail: { value: 1 },
      },
    });
    this.particles = new THREE.Mesh(g, this.pMat);
    this.particles.frustumCulled = false;
    this.particles.renderOrder = 5;
    this.scene.add(this.particles);
  }

  private buildNpu(): void {
    const npu = this.blockById.get('npu');
    if (!npu) return;
    const tiles = npu.units;
    const cols = Math.ceil(Math.sqrt(tiles));
    const rows = Math.ceil(tiles / cols);
    const n = 8;
    const cell = new THREE.BoxGeometry(1, 1, 1);
    const mat = new THREE.MeshBasicMaterial({ color: '#ffffff', toneMapped: false });
    const im = new THREE.InstancedMesh(cell, mat, tiles * n * n);
    const tw = (npu.w - 0.5) / cols;
    const td = (npu.d - 0.5) / rows;
    const pitch = Math.min(tw, td) / (n + 1.6);
    const m4 = new THREE.Matrix4();
    let k = 0;
    this.npuCellInfo = [];
    for (let t = 0; t < tiles; t++) {
      const tx = npu.cx - npu.w / 2 + 0.25 + (t % cols + 0.5) * tw;
      const tz = npu.cz - npu.d / 2 + 0.25 + (Math.floor(t / cols) + 0.5) * td;
      for (let i = 0; i < n; i++) {
        for (let j = 0; j < n; j++) {
          m4.makeScale(pitch * 0.66, 0.04, pitch * 0.66);
          m4.setPosition(tx + (j - (n - 1) / 2) * pitch, 0, tz + (i - (n - 1) / 2) * pitch);
          im.setMatrixAt(k, m4);
          im.setColorAt(k, new THREE.Color('#1a2233'));
          this.npuCellInfo.push({ tile: t, i, j });
          k++;
        }
      }
    }
    im.renderOrder = 3;
    this.scene.add(im);
    this.npuCells = im;
  }

  private buildDram(): void {
    const dram = this.blockById.get('dram');
    if (!dram || !this.store) return;
    const ctrls = Number(dram.info.controllers ?? 2) || 2;
    for (let c = 0; c < ctrls; c++) {
      const x = dram.cx - dram.w / 2 + ((c + 0.5) / ctrls) * dram.w;
      const sleeve = new THREE.Mesh(
        new THREE.CylinderGeometry(0.2, 0.2, 2.4, 32, 1, true),
        new THREE.MeshPhysicalMaterial({ color: '#c5d3ea', transparent: true, opacity: 0.08, roughness: 0.1, depthWrite: false, side: THREE.DoubleSide }),
      );
      sleeve.position.set(x, 1.2, dram.cz);
      this.scene.add(sleeve);
      const geo = new THREE.CylinderGeometry(0.15, 0.15, 1, 32);
      geo.translate(0, 0.5, 0);
      const mat = new THREE.MeshBasicMaterial({ color: '#ffb547', toneMapped: false }) as unknown as THREE.MeshStandardMaterial;
      const mesh = new THREE.Mesh(geo, mat);
      mesh.position.set(x, 0.05, dram.cz);
      this.scene.add(mesh);
      const el = document.createElement('div');
      el.className = 'die-label die-label-small';
      const lbl = new CSS2DObject(el);
      lbl.position.set(x, 0.1, dram.cz + dram.d / 2 - 0.35);
      this.scene.add(lbl);
      this.dramCols.push({ mesh, mat, h: new Spring(0.05, 140, 20), label: el, ctrl: c });
    }
  }

  /** Slide the die horizontally (px) to make room for an overlay card, with a spring. */
  setShift(px: number): void {
    this.shift.target = px;
  }

  private fit: { cx: number; width: number; until: number } | null = null;
  /** Ease the camera so the die spans `width` px centered at screen x `cx` (runs briefly after layout changes). */
  fitTo(cx: number, width: number): void {
    this.fit = { cx, width: Math.max(220, width), until: performance.now() + 900 };
  }

  private applyFit(): void {
    const f = this.fit;
    if (!f) return;
    if (performance.now() > f.until) { this.fit = null; return; }
    const e = this.dieScreen(this.size.w, this.size.h);
    // horizontal: move the view offset so the die center lands on cx
    this.shift.target = this.shift.target + (f.cx - e.cx) * 0.18;
    // size: dolly toward the target width
    const ratio = (2 * e.rx) / f.width;
    if (Math.abs(ratio - 1) > 0.01) {
      const dir = this.camera.position.clone().sub(this.controls.target);
      const d = dir.length();
      const nd = THREE.MathUtils.clamp(d * (1 + (ratio - 1) * 0.15), this.controls.minDistance, this.controls.maxDistance);
      this.camera.position.copy(this.controls.target).add(dir.multiplyScalar(nd / d));
    }
  }

  resize(w: number, h: number): void {
    this.size = { w, h };
    this.renderer.setSize(w, h, false);
    this.composer.setSize(w, h);
    this.bloom.resolution.set(w / 2, h / 2);
    this.labels.setSize(w, h);
    this.camera.aspect = w / Math.max(1, h);
    this.camera.updateProjectionMatrix();
  }

  /** Screen-space center and radii of the die (for the deadline ring layout). */
  dieScreen(w: number, h: number): { cx: number; cy: number; rx: number; ry: number } {
    const pts = [[-1, -1], [1, -1], [1, 1], [-1, 1]].map(([a, b]) => {
      const v = new THREE.Vector3((a * DIE) / 2, 0, (b * DIE) / 2).project(this.camera);
      void w;
      return [(v.x * 0.5 + 0.5) * w, (-v.y * 0.5 + 0.5) * h];
    });
    const xs = pts.map((p) => p[0]);
    const ys = pts.map((p) => p[1]);
    const minx = Math.min(...xs), maxx = Math.max(...xs), miny = Math.min(...ys), maxy = Math.max(...ys);
    return { cx: (minx + maxx) / 2, cy: (miny + maxy) / 2, rx: (maxx - minx) / 2, ry: (maxy - miny) / 2 };
  }

  /** Rebuild particle instances for xfers around time t. speed = sim s per wall s. */
  private refreshParticles(t: number, speed: number): void {
    const st = this.store!;
    const travel = Math.max(speed * (speed > 1e-3 ? 0.35e12 : 0.6e12), 4000 * 3); // ps of sim time a particle takes, at least ~12 ns
    // level of detail in time: at fast playback one particle per transfer (link glow carries volume),
    // at slow motion each transfer becomes a stream of particles
    const spacing = Math.max(1, speed * 0.008e12); // one particle per ~8 ms of wall time per transfer
    const ahead = Math.max(speed * 1.6e12, 2e5);
    const behind = travel * 1.4;
    const w = this.window;
    const needs = w.t0 < 0 || t < w.t0 + behind * 0.25 || t + travel > w.t1 || Math.abs(Math.log(travel / Math.max(1, w.travel))) > 0.4 || !w.complete;
    if (!needs) return;
    const t0 = Math.max(0, t - behind);
    const t1 = t + ahead;
    void st.ensureEvents(t0, t1);
    const blocks = st.loadedBlocks(t0, t1);
    const base = t;
    let k = 0;
    const src = this.aSrc.array as Float32Array, dst = this.aDst.array as Float32Array, tt = this.aT0.array as Float32Array;
    const col = this.aColor.array as Float32Array, size = this.aSize.array as Float32Array;
    const nx = this.dims[0];
    // scale: ns relative to base keeps float32 precision near "now"
    const unit = 1e3;
    let total = 0;
    for (const b of blocks) for (let i = 0; i < b.n; i++) if (b.kind[i] === KIND.xfer) total++;
    const budget = MAX_PARTICLES;
    outer: for (const b of blocks) {
      for (let i = 0; i < b.n; i++) {
        if (b.kind[i] !== KIND.xfer) continue;
        const s = b.src[i], d = b.dst[i];
        if (s === NONE16 || d === NONE16) continue;
        const xs = b.t[i], xd = Math.max(b.dur[i], 1000);
        if (xs + xd + travel * 1.12 < t0 || xs > t1) continue;
        let np = Math.max(1, Math.min(600, Math.round((xd / spacing) * Math.min(1, b.bytes[i] / 65536))));
        if (total * 8 > budget) np = Math.max(1, Math.floor(np * budget / (total * 10)));
        const c = this.taskColors[b.task[i]] ?? this.tmpColor.set('#c8d2de');
        const sz = 0.075 + Math.min(0.07, b.bytes[i] / 4e7);
        for (let q = 0; q < np; q++) {
          if (k >= MAX_PARTICLES) break outer;
          const te = xs + (xd * q) / np;
          if (te + travel * 1.12 < t0 || te > t1) continue;
          src[k * 2] = s % nx; src[k * 2 + 1] = Math.floor(s / nx);
          dst[k * 2] = d % nx; dst[k * 2 + 1] = Math.floor(d / nx);
          tt[k] = (te - base) / unit;
          col[k * 3] = c.r; col[k * 3 + 1] = c.g; col[k * 3 + 2] = c.b;
          size[k] = sz;
          k++;
        }
      }
    }
    for (const a of [this.aSrc, this.aDst, this.aT0, this.aColor, this.aSize]) {
      a.clearUpdateRanges();
      a.addUpdateRange(0, k * a.itemSize);
      a.needsUpdate = true;
    }
    this.pGeo.instanceCount = k;
    this.particleCount = k;
    // keep total light roughly constant: estimate how many particles are in flight at once
    const inFlight = k * Math.min(1, (travel * 1.12) / Math.max(1, t1 - t0));
    this.pMat.uniforms.uGain.value = Math.max(0.03, Math.min(1, 900 / Math.max(1, inFlight)));
    this.window = { base, t0, t1, travel, complete: st.windowLoaded(t0, t1) };
    this.pMat.uniforms.uTravel.value = travel / unit;
  }

  private scanActivity(t: number): void {
    const st = this.store!;
    this.unitActivity.clear();
    for (const b of st.loadedBlocks(t - 5e10, t)) {
      for (let i = 0; i < b.n; i++) {
        if (b.kind[i] !== KIND.op) continue;
        const s = b.t[i];
        if (s > t) continue;
        const e = s + b.dur[i];
        if (e < t) continue;
        this.unitActivity.set(`${b.block[i]}:${b.unit[i]}`, { task: b.task[i], progress: (t - s) / Math.max(1, b.dur[i]), layer: b.aux[i], job: b.job[i] });
      }
    }
  }

  update(t: number, speed: number, dt: number): void {
    const st = this.store;
    if (!st) return;
    const m = st.manifest;
    this.refreshParticles(t, speed);
    this.pMat.uniforms.uTime.value = (t - this.window.base) / 1e3;
    const camDist = this.camera.position.distanceTo(this.controls.target);
    const lodK = THREE.MathUtils.smoothstep(camDist, 24, 36);
    this.pMat.uniforms.uOpacity.value = 1 - 0.85 * lodK;
    this.scanActivity(t);

    const vnom = Number(m.soc.process.v_nominal ?? 0.75);
    const bin = Math.max(0, Math.min(m.lod.bins - 1, Math.floor(t / m.lod.bin_ps)));
    const nUnits = m.lod.units.length;
    for (const b of this.blocks) {
      // activity: fraction of units running an op now (falls back to LOD busy fraction)
      let active = 0;
      for (let u = 0; u < b.units; u++) if (this.unitActivity.has(`${b.index}:${u}`)) active++;
      let frac = active / b.units;
      if (speed > 0.05) {
        let busy = 0, cnt = 0;
        m.lod.units.forEach((u, ui) => { if (u.block_index === b.index) { busy += st.lodBusy[bin * nUnits + ui] / 255; cnt++; } });
        if (cnt) frac = busy / cnt;
      }
      if (b.info.id === 'sys_cache' || b.info.id === 'dram') {
        const q = (st.sample('dram_q.0', t) + st.sample('dram_q.1', t)) / 128;
        frac = Number.isFinite(q) ? Math.min(1, q * 1.6) : 0;
      }
      b.glow.target = frac;
      b.glow.step(dt);
      const p = st.sample(`power.${b.domain}`, t);
      const dom = m.soc.power_domains[b.domain];
      const v = st.sample(`volt.${b.domain}`, t);
      const thr = st.sample(`throttle.${b.domain}`, t) > 0.5;
      const f = st.sample(`freq.${b.domain}`, t);
      const temp = st.sample(`temp.${b.domain}`, t);
      b.height.target = 0.14 + (Number.isFinite(p) ? Math.min(0.75, p / 9) : 0);
      b.mesh.scale.y = Math.max(0.05, b.height.step(dt));
      let rgb: [number, number, number];
      if (this.heatOverlay && Number.isFinite(temp)) {
        const k = THREE.MathUtils.clamp((temp - 25) / 70, 0, 1);
        rgb = [0.25 + 0.75 * k, 0.45 * (1 - k) + 0.25 * k * (1 - k) * 4, 1 - k];
      } else {
        // fixed-voltage domains (v_min == v_max) sit at their own nominal
        const vref = dom ? Math.min(vnom, dom.v_max) : vnom;
        const fixed = dom ? dom.v_max - dom.v_min < 1e-3 : false;
        rgb = voltageTint(fixed ? vref : Number.isFinite(v) ? v : vref, fixed ? vref - 0.25 : dom?.v_min ?? 0.5, vref, thr);
      }
      const g = Math.max(0, b.glow.value);
      const S = THREE.SRGBColorSpace;
      b.mat.color.setRGB(0.1 + 0.25 * rgb[0], 0.12 + 0.25 * rgb[1], 0.16 + 0.25 * rgb[2], S);
      b.mat.emissive.setRGB(rgb[0] * 0.06, rgb[1] * 0.06, rgb[2] * 0.06, S);
      // dark slate glass body; the voltage tint lives in the inner light and the rim
      const mix = 0.14 + 0.5 * g;
      const base = [0.075, 0.095, 0.135];
      b.core.color.setRGB(base[0] + (rgb[0] * 0.92 - base[0]) * mix, base[1] + (rgb[1] * 0.92 - base[1]) * mix, base[2] + (rgb[2] * 0.92 - base[2]) * mix, S);
      const em = b.edges.material as THREE.LineBasicMaterial;
      em.color.setRGB(rgb[0], rgb[1], rgb[2], S);
      em.opacity = 0.5 + 0.45 * g;
      if (this.pulse && this.pulse.block === b) {
        this.pulse.t += dt;
        const k = Math.sin(this.pulse.t * 6) * 0.5 + 0.5;
        b.mat.emissive.setRGB(1.0 * k, 0.18 * k, 0.22 * k);
        b.core.color.setRGB(0.45 + 0.5 * k, 0.1, 0.14, THREE.SRGBColorSpace);
        if (this.pulse.t > 6) this.pulse = null;
      }
      b.label.position.y = b.mesh.scale.y + 0.05;
      b.label.visible = this.showLabels;
      if (this.showLabels) {
        const parts: string[] = [];
        if (Number.isFinite(v) && b.info.id !== 'dram') parts.push(`${v.toFixed(2)} V`);
        if (Number.isFinite(p)) parts.push(`${p.toFixed(2)} W`);
        void f;
        const txt = parts.join('   ');
        if (b.labelSub.textContent !== txt) b.labelSub.textContent = txt;
      }
    }

    // NoC link glow from per-ms link bytes (LOD flow ribbons)
    const nl = m.lod.links.length;
    const cap = 48e6; // bytes per ms per link: 256 bit * 1.5 GHz
    for (const lg of this.linkGlow) {
      const i1 = this.linkIndex.get(`${lg.a}-${lg.b}`) ?? -1;
      const i2 = this.linkIndex.get(`${lg.b}-${lg.a}`) ?? -1;
      let bytes = 0;
      for (let k = -2; k <= 0; k++) {
        const bb = Math.max(0, bin + k);
        if (i1 >= 0) bytes += st.lodLinks[bb * nl + i1];
        if (i2 >= 0) bytes += st.lodLinks[bb * nl + i2];
      }
      lg.util.target = Math.min(1, bytes / 3 / cap);
      const u = Math.max(0, lg.util.step(dt));
      lg.mat.opacity = Math.min(0.7, (0.06 + 0.7 * Math.sqrt(u)) * (0.45 + 0.55 * lodK));
      const s = 1 + 2.2 * Math.sqrt(u) * (0.5 + lodK);
      lg.mesh.scale.set(s, 1, s);
      lg.mat.color.setRGB(0.45 + 0.55 * u, 0.62 + 0.2 * u, 1.0);
    }

    // NPU tiles: diagonal wavefront per active layer
    const npu = this.blockById.get('npu');
    if (this.npuCells && npu) {
      const top = npu.mesh.scale.y + 0.03;
      const m4 = new THREE.Matrix4();
      const tmp = new THREE.Vector3();
      const sc = new THREE.Vector3();
      const q = new THREE.Quaternion();
      this.npuCellInfo.forEach((c, k) => {
        this.npuCells!.getMatrixAt(k, m4);
        m4.decompose(tmp, q, sc);
        tmp.y = top;
        const act = this.unitActivity.get(`${npu.index}:${c.tile}`);
        let inten = 0.0;
        let colr = this.tmpColor.set('#3d4a66');
        if (act) {
          const front = act.progress * 18 - 2;
          const dd = c.i + c.j - front;
          inten = 0.25 + 0.9 * Math.exp(-dd * dd * 0.35) + 0.15 * ((act.layer + c.i) % 3 === 0 ? 1 : 0);
          colr = this.taskColors[act.task] ?? colr;
        }
        sc.y = 0.02 + 0.12 * Math.min(1, inten);
        m4.compose(tmp, q, sc);
        this.npuCells!.setMatrixAt(k, m4);
        this.npuCells!.setColorAt(k, act ? this.tmpColor.copy(colr).multiplyScalar(Math.min(2.4, 0.15 + inten * 1.7)) : this.tmpColor.set('#2b3954'));
      });
      this.npuCells.instanceMatrix.needsUpdate = true;
      if (this.npuCells.instanceColor) this.npuCells.instanceColor.needsUpdate = true;
    }

    // DRAM queue columns
    for (const c of this.dramCols) {
      const q = st.sample(`dram_q.${c.ctrl}`, t);
      const qq = Number.isFinite(q) ? q : 0;
      c.h.target = 0.05 + (qq / 64) * 2.3;
      c.mesh.scale.y = Math.max(0.02, c.h.step(dt));
      const k = Math.min(1, qq / 40);
      const br = 0.75 + 0.6 * k;
      c.mat.color.setRGB(1.0 * br, (0.72 - 0.52 * k) * br, (0.18 - 0.08 * k) * br, THREE.SRGBColorSpace);
      const txt = Number.isFinite(q) ? `Queue ${c.ctrl}  ${qq.toFixed(0)}` : '';
      if (c.label.textContent !== txt) c.label.textContent = txt;
      c.label.style.display = this.showLabels ? '' : 'none';
    }

    // critical path animation
    for (const pa of this.pathAnim) {
      pa.t += dt;
      const k = Math.min(1, pa.t / pa.dur);
      const e = 1 - Math.pow(1 - k, 3);
      pa.mesh.geometry.setDrawRange(0, Math.floor((pa.total * e) / 3) * 3);
      const mat = pa.mesh.material as THREE.MeshBasicMaterial;
      if (pa.t > pa.dur + pa.hold) mat.opacity = Math.max(0, 1 - (pa.t - pa.dur - pa.hold) / 1.5);
    }
    this.pathAnim = this.pathAnim.filter((pa) => {
      if (pa.t > pa.dur + pa.hold + 1.5) { pa.mesh.removeFromParent(); pa.mesh.geometry.dispose(); return false; }
      return true;
    });

    this.applyFit();
    const es = this.dieScreen(this.size.w, this.size.h);
    this.labelEl.classList.toggle('compact', es.rx * 2 < 600);
    const sh = this.shift.step(dt);
    if (Math.abs(sh) > 0.5) this.camera.setViewOffset(this.size.w, this.size.h, -sh, 0, this.size.w, this.size.h);
    else this.camera.clearViewOffset();
    this.controls.update();
    this.composer.render();
    this.labels.render(this.scene, this.camera);
  }

  private unitWorld(engine: string): THREE.Vector3 | null {
    const mm = /^([a-z_]+?)(\d+)$/.exec(engine);
    const id = mm ? mm[1] : engine;
    const u = mm ? Number(mm[2]) : 0;
    const b = this.blockById.get(id);
    if (!b) return null;
    if (id === 'npu') {
      const cols = Math.ceil(Math.sqrt(b.units));
      const rows = Math.ceil(b.units / cols);
      const tw = (b.w - 0.5) / cols, td = (b.d - 0.5) / rows;
      return new THREE.Vector3(b.cx - b.w / 2 + 0.25 + (u % cols + 0.5) * tw, b.mesh.scale.y + 0.2, b.cz - b.d / 2 + 0.25 + (Math.floor(u / cols) + 0.5) * td);
    }
    return new THREE.Vector3(b.cx, b.mesh.scale.y + 0.2, b.cz);
  }

  /** Animate a job's critical path: DRAM -> NoC route -> engine unit, op by op. */
  showCriticalPath(cp: CritOp[], routes: { src: number; dst: number }[], color: string, dominant: string): void {
    this.clearPath();
    const pts: THREE.Vector3[] = [];
    const nx = this.dims[0];
    const seen = new Set<string>();
    for (const r of routes) {
      const key = `${r.src}-${r.dst}`;
      if (seen.has(key)) continue;
      seen.add(key);
      const sx = r.src % nx, sy = Math.floor(r.src / nx), dx = r.dst % nx, dy = Math.floor(r.dst / nx);
      pts.push(nodeWorld(sx, sy, this.dims).setY(Y_NOC + 0.12));
      pts.push(nodeWorld(dx, sy, this.dims).setY(Y_NOC + 0.12));
      pts.push(nodeWorld(dx, dy, this.dims).setY(Y_NOC + 0.12));
    }
    for (const op of cp) {
      const p = this.unitWorld(op.engine);
      if (p && (pts.length === 0 || pts[pts.length - 1].distanceTo(p) > 0.05)) pts.push(p);
    }
    if (pts.length >= 2) {
      const path = new THREE.CurvePath<THREE.Vector3>();
      for (let i = 0; i + 1 < pts.length; i++) if (pts[i].distanceTo(pts[i + 1]) > 1e-3) path.add(new THREE.LineCurve3(pts[i], pts[i + 1]));
      const segs = Math.max(64, path.curves.length * 24);
      const geo = new THREE.TubeGeometry(path, segs, 0.07, 10, false);
      const mat = new THREE.MeshBasicMaterial({ color: new THREE.Color(color).multiplyScalar(2.2), transparent: true, opacity: 1, toneMapped: false, depthTest: false });
      const mesh = new THREE.Mesh(geo, mat);
      mesh.renderOrder = 10;
      this.pathGroup.add(mesh);
      this.pathAnim.push({ mesh, total: geo.index ? geo.index.count : 0, t: 0, dur: 3.2, hold: 8 });
    }
    const target = dominant === 'dram' ? 'dram' : dominant === 'noc' ? 'sys_cache' : (cp[0]?.engine.replace(/\d+$/, '') ?? '');
    const b = this.blockById.get(target);
    if (b) this.pulse = { block: b, t: 0 };
  }

  clearPath(): void {
    for (const pa of this.pathAnim) { pa.mesh.removeFromParent(); pa.mesh.geometry.dispose(); }
    this.pathAnim = [];
    this.pulse = null;
  }

  focusOverview(): void {
    this.camera.position.set(0.5, 16.4, 14.0);
    this.controls.target.set(0, 0.2, 0.4);
  }
}
