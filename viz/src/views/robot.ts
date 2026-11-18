// Robot viewport: MuJoCo scene geometry + recorded body poses.
import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import { RoomEnvironment } from 'three/examples/jsm/environments/RoomEnvironment.js';
import { gunzip } from '../replay/decode';
import type { ReplayStore } from '../replay/store';

interface SceneGeom {
  body: string; type: string; size: number[]; pos: number[]; quat: number[]; rgba: number[]; mesh?: number; role?: string;
}
interface SceneMesh { name: string; vcount: number; icount: number; pos_offset: number; idx_offset: number; bbox_min: number[]; bbox_max: number[] }
interface SceneJson {
  format: string; bodies: string[]; meshes: SceneMesh[]; mesh_file: string; geoms: SceneGeom[];
  conveyor?: { x: number; width: number; top_z: number; y0: number; y1: number };
}

// cosim/SCENE_FORMAT.md ("glasschip-scene"): float32 vertices + normals, uint32 faces in scene.bin
interface ArrDesc { offset: number; bytes: number; dtype: string; shape: number[] }
interface CosimScene {
  format: 'glasschip-scene'; bin_file: string;
  bodies: { id: number; name: string; xpos: number[]; xquat: number[]; mocap?: boolean }[];
  geoms: { body: string; type: string; size: number[]; pos: number[]; quat: number[]; rgba: number[]; role?: string; mesh?: number }[];
  meshes: { name: string; n_vert: number; n_face: number; vertices: ArrDesc; normals: ArrDesc; faces: ArrDesc }[];
}

export class RobotView {
  readonly renderer: THREE.WebGLRenderer;
  readonly scene = new THREE.Scene();
  readonly camera: THREE.PerspectiveCamera;
  private controls: OrbitControls;
  private bodies = new Map<string, THREE.Object3D>();
  private bodyOrder: (THREE.Object3D | null)[] = [];
  private sceneUrl = '';
  private belt: THREE.Mesh | null = null;
  private beltTex: THREE.CanvasTexture | null = null;
  private store: ReplayStore | null = null;
  private qa = new THREE.Quaternion();
  private qb = new THREE.Quaternion();
  private flash: THREE.Mesh;
  private flashT = -1;
  private lastEventIdx = -1;

  constructor(canvas: HTMLCanvasElement) {
    this.renderer = new THREE.WebGLRenderer({ canvas, antialias: true, powerPreference: 'high-performance' });
    this.renderer.setPixelRatio(Math.min(devicePixelRatio, 1.5));
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.05;
    this.renderer.shadowMap.enabled = true;
    this.renderer.shadowMap.type = THREE.PCFShadowMap;
    const pmrem = new THREE.PMREMGenerator(this.renderer);
    this.scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
    this.scene.environmentIntensity = 0.6;
    this.scene.background = new THREE.Color('#0d1118');
    this.scene.fog = new THREE.Fog('#0d1118', 3.2, 7.5);

    this.camera = new THREE.PerspectiveCamera(34, 1, 0.05, 50);
    this.camera.up.set(0, 0, 1);
    this.camera.position.set(1.95, -1.3, 1.22);
    this.controls = new OrbitControls(this.camera, canvas);
    this.controls.target.set(0.26, -0.06, 0.26);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.08;
    this.controls.minDistance = 0.8;
    this.controls.maxDistance = 5;
    this.controls.maxPolarAngle = Math.PI * 0.49;
    this.controls.update();

    const hemi = new THREE.HemisphereLight('#dbe7ff', '#1a1d24', 0.35);
    this.scene.add(hemi);
    const key = new THREE.DirectionalLight('#fff4e6', 2.4);
    key.position.set(1.6, -1.2, 3.2);
    key.castShadow = true;
    key.shadow.mapSize.set(1536, 1536);
    key.shadow.camera.left = -1.5; key.shadow.camera.right = 1.5;
    key.shadow.camera.top = 1.5; key.shadow.camera.bottom = -1.5;
    key.shadow.camera.near = 0.5; key.shadow.camera.far = 8;
    key.shadow.bias = -0.0004;
    key.shadow.normalBias = 0.02;
    key.shadow.radius = 6;
    key.shadow.blurSamples = 16;
    this.scene.add(key);
    const rim = new THREE.DirectionalLight('#8fb5ff', 1.1);
    rim.position.set(-2, 2, 1.5);
    this.scene.add(rim);

    // floor: soft radial falloff so the robot sits on a lit stage, no hard horizon
    const floorTex = this.radialTexture();
    const floor = new THREE.Mesh(
      new THREE.CircleGeometry(6, 96),
      new THREE.MeshStandardMaterial({ color: '#3a4354', roughness: 0.9, metalness: 0, map: floorTex }),
    );
    floor.receiveShadow = true;
    this.scene.add(floor);

    this.flash = new THREE.Mesh(
      new THREE.RingGeometry(0.06, 0.075, 48),
      new THREE.MeshBasicMaterial({ color: '#ff4d5e', transparent: true, opacity: 0, depthWrite: false }),
    );
    this.flash.visible = false;
    this.scene.add(this.flash);
  }

  private radialTexture(): THREE.CanvasTexture {
    const c = document.createElement('canvas');
    c.width = c.height = 256;
    const g = c.getContext('2d')!;
    const grd = g.createRadialGradient(128, 128, 10, 128, 128, 128);
    grd.addColorStop(0, '#ffffff');
    grd.addColorStop(0.18, '#c4cad4');
    grd.addColorStop(0.5, '#4a5160');
    grd.addColorStop(1, '#0d1118');
    g.fillStyle = grd;
    g.fillRect(0, 0, 256, 256);
    const t = new THREE.CanvasTexture(c);
    t.colorSpace = THREE.SRGBColorSpace;
    return t;
  }

  private beltTexture(): THREE.CanvasTexture {
    const c = document.createElement('canvas');
    c.width = 64; c.height = 256;
    const g = c.getContext('2d')!;
    g.fillStyle = '#1e2229';
    g.fillRect(0, 0, 64, 256);
    g.fillStyle = '#2a2f38';
    for (let y = 0; y < 256; y += 32) g.fillRect(0, y, 64, 3);
    const t = new THREE.CanvasTexture(c);
    t.wrapS = t.wrapT = THREE.RepeatWrapping;
    t.colorSpace = THREE.SRGBColorSpace;
    return t;
  }

  async setStore(store: ReplayStore): Promise<void> {
    this.store = store;
    this.lastEventIdx = -1;
    const r = store.manifest.robot;
    if (!r || !r.scene) return;
    const url = store.url(r.scene.endsWith('/') ? r.scene : r.scene + '/');
    if (url !== this.sceneUrl) {
      await this.loadScene(url);
      this.sceneUrl = url;
    }
    this.bodyOrder = r.bodies.map((n) => this.bodies.get(n) ?? null);
    const used = new Set(r.bodies);
    for (const [name, o] of this.bodies) o.visible = used.has(name) || !(name.startsWith('obj_') || name === 'human');
  }

  private async loadScene(url: string): Promise<void> {
    for (const b of this.bodies.values()) b.removeFromParent();
    this.bodies.clear();
    const anyJson = (await (await fetch(url + 'scene.json')).json()) as SceneJson | CosimScene;
    const fetchBin = async (f: string): Promise<ArrayBuffer> => {
      const raw = await (await fetch(url + f)).arrayBuffer();
      const u8 = new Uint8Array(raw, 0, 2);
      return u8[0] === 0x1f && u8[1] === 0x8b ? gunzip(raw) : raw;
    };
    if (anyJson.format === 'glasschip-scene') {
      const cs = anyJson as CosimScene;
      const bin = await fetchBin(cs.bin_file);
      const cgeos = cs.meshes.map((m) => {
        const g = new THREE.BufferGeometry();
        g.setAttribute('position', new THREE.BufferAttribute(new Float32Array(bin.slice(m.vertices.offset, m.vertices.offset + m.vertices.bytes)), 3));
        g.setAttribute('normal', new THREE.BufferAttribute(new Float32Array(bin.slice(m.normals.offset, m.normals.offset + m.normals.bytes)), 3));
        g.setIndex(new THREE.BufferAttribute(new Uint32Array(bin.slice(m.faces.offset, m.faces.offset + m.faces.bytes)), 1));
        return g;
      });
      this.buildFromGeoms(cs.bodies.map((b) => b.name), cs.geoms.filter((g) => g.role !== 'floor'), cgeos);
      for (const b of cs.bodies) {
        const o = this.bodies.get(b.name);
        if (o && b.xpos) { o.position.set(b.xpos[0], b.xpos[1], b.xpos[2]); o.quaternion.set(b.xquat[1], b.xquat[2], b.xquat[3], b.xquat[0]); }
      }
      return;
    }
    const sj = anyJson as SceneJson;
    const buf = await fetchBin(sj.mesh_file);
    const geos = sj.meshes.map((m) => {
      const q = new Uint16Array(buf, m.pos_offset, m.vcount * 3);
      const pos = new Float32Array(m.vcount * 3);
      for (let i = 0; i < m.vcount; i++) {
        for (let k = 0; k < 3; k++) {
          pos[i * 3 + k] = m.bbox_min[k] + (q[i * 3 + k] / 65535) * (m.bbox_max[k] - m.bbox_min[k]);
        }
      }
      const idx = new Uint32Array(buf, m.idx_offset, m.icount);
      const g = new THREE.BufferGeometry();
      g.setAttribute('position', new THREE.BufferAttribute(pos, 3));
      g.setIndex(new THREE.BufferAttribute(new Uint32Array(idx), 1));
      g.computeVertexNormals();
      return g;
    });
    this.buildFromGeoms(sj.bodies, sj.geoms, geos);
  }

  private buildFromGeoms(bodyNames: string[], geomList: SceneGeom[], geos: THREE.BufferGeometry[]): void {
    for (const name of bodyNames) {
      const o = new THREE.Object3D();
      o.name = name;
      o.matrixAutoUpdate = true;
      this.scene.add(o);
      this.bodies.set(name, o);
    }
    const matCache = new Map<string, THREE.Material>();
    for (const g of geomList) {
      const parent = this.bodies.get(g.body);
      if (!parent) continue;
      let geo: THREE.BufferGeometry;
      switch (g.type) {
        case 'mesh': geo = geos[g.mesh ?? 0]; break;
        case 'box': geo = new THREE.BoxGeometry(g.size[0] * 2, g.size[1] * 2, g.size[2] * 2); break;
        case 'sphere': geo = new THREE.SphereGeometry(g.size[0], 32, 16); break;
        case 'ellipsoid': geo = new THREE.SphereGeometry(1, 32, 16).scale(g.size[0], g.size[1], g.size[2]); break;
        case 'cylinder': geo = new THREE.CylinderGeometry(g.size[0], g.size[0], g.size[1] * 2, 32).rotateX(Math.PI / 2); break;
        case 'capsule': geo = new THREE.CapsuleGeometry(g.size[0], g.size[1], 8, 24).rotateX(Math.PI / 2); break;
        default: continue;
      }
      const key = `${g.rgba.join(',')}|${g.role}`;
      let mat = matCache.get(key);
      if (!mat) {
        const col = new THREE.Color(g.rgba[0], g.rgba[1], g.rgba[2]);
        const dark = g.rgba[0] < 0.3;
        mat = new THREE.MeshStandardMaterial({
          color: col,
          roughness: g.role === 'robot' ? (dark ? 0.45 : 0.28) : g.role === 'object' ? 0.4 : 0.7,
          metalness: g.role === 'rail' || g.role === 'leg' ? 0.6 : 0.05,
          transparent: g.rgba[3] < 1,
          opacity: g.rgba[3],
          depthWrite: g.rgba[3] >= 1,
        });
        matCache.set(key, mat);
      }
      const mesh = new THREE.Mesh(geo, mat);
      mesh.position.set(g.pos[0], g.pos[1], g.pos[2]);
      mesh.quaternion.set(g.quat[1], g.quat[2], g.quat[3], g.quat[0]);
      mesh.castShadow = g.role !== 'human';
      mesh.receiveShadow = true;
      if (g.role === 'belt') {
        this.beltTex = this.beltTexture();
        const len = g.size[1] * 2;
        this.beltTex.repeat.set(1, len / 0.25);
        (mat as THREE.MeshStandardMaterial).map = this.beltTex;
        (mat as THREE.MeshStandardMaterial).color.set('#ffffff');
        this.belt = mesh;
      }
      parent.add(mesh);
    }
    void this.belt;
  }

  resize(w: number, h: number): void {
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / Math.max(1, h);
    this.camera.updateProjectionMatrix();
  }

  update(t: number, dtWall: number): void {
    const st = this.store;
    const r = st?.manifest.robot;
    if (st && r) {
      const fx = (t / 1e12) * r.fps;
      const f0 = Math.floor(fx);
      void st.ensureRobot(f0 + 30);
      const a = st.robotFrame(f0);
      const b = st.robotFrame(f0 + 1) ?? a;
      const k = fx - f0;
      if (a && b) {
        for (let i = 0; i < this.bodyOrder.length; i++) {
          const o = this.bodyOrder[i];
          if (!o) continue;
          const p = i * 7;
          o.position.set(a[p] + (b[p] - a[p]) * k, a[p + 1] + (b[p + 1] - a[p + 1]) * k, a[p + 2] + (b[p + 2] - a[p + 2]) * k);
          this.qa.set(a[p + 4], a[p + 5], a[p + 6], a[p + 3]);
          this.qb.set(b[p + 4], b[p + 5], b[p + 6], b[p + 3]);
          o.quaternion.slerpQuaternions(this.qa, this.qb, k);
        }
      }
      if (this.beltTex) this.beltTex.offset.y = -((t / 1e12) * 0.2) / 0.25;
      // grasp-miss flash under the arm
      const evs = r.events;
      let idx = -1;
      for (let i = 0; i < evs.length; i++) if (evs[i].t_ps <= t) idx = i;
      if (idx !== this.lastEventIdx) {
        if (idx >= 0 && evs[idx].kind === 'grasp_miss' && t - evs[idx].t_ps < 2e11) {
          const obj = this.bodies.get(String(evs[idx].object));
          if (obj) this.flash.position.set(obj.position.x, obj.position.y, 0.175);
          this.flashT = 0;
        }
        this.lastEventIdx = idx;
      }
    }
    if (this.flashT >= 0) {
      this.flashT += dtWall;
      const k = this.flashT / 1.4;
      this.flash.visible = k < 1;
      (this.flash.material as THREE.MeshBasicMaterial).opacity = Math.max(0, 1 - k) * 0.9;
      this.flash.scale.setScalar(1 + k * 2.2);
      if (k >= 1) this.flashT = -1;
    }
    this.controls.update();
    this.renderer.render(this.scene, this.camera);
  }
}
