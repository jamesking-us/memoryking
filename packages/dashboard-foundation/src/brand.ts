/** Original crown mesh; no external texture, font, renderer or network dependency. */
type Point = [number, number, number];
interface Face {
  points: Point[];
  color: string;
}
export function crownMesh(): Face[] {
  const faces: Face[] = [];
  const extrude = (
    outline: [number, number][],
    depth: number,
    front: string,
    side: string,
  ) => {
    const a = outline.map(([x, y]) => [x, y, depth / 2] as Point),
      b = outline.map(([x, y]) => [x, y, -depth / 2] as Point);
    faces.push({ points: a, color: front }, { points: b, color: side });
    for (let i = 0; i < a.length; i++) {
      const j = (i + 1) % a.length;
      faces.push({ points: [a[i]!, a[j]!, b[j]!, b[i]!], color: side });
    }
  };
  extrude(
    [
      [-34, -12],
      [-43, -49],
      [-17, -35],
      [0, -61],
      [17, -35],
      [43, -49],
      [34, -12],
    ],
    18,
    "#f5cb77",
    "#a66b29",
  );
  for (let i = 0; i < 3; i++) {
    const w = 38 + i * 5,
      y = -5 + i * 21;
    extrude(
      [
        [-w, y],
        [w, y],
        [w, y + 15],
        [-w, y + 15],
      ],
      26 + i * 5,
      ["#c9adff", "#a77bdf", "#7955b5"][i]!,
      "#50346f",
    );
  }
  return faces;
}
export function installCrown(button: HTMLButtonElement): void {
  const canvas = document.createElement("canvas");
  canvas.width = 320;
  canvas.height = 320;
  canvas.setAttribute("aria-hidden", "true");
  canvas.hidden = true;
  const img = button.querySelector("img")!;
  button.prepend(canvas);
  let motionAllowed = true,
    frames = 0;
  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "quiet";
  toggle.textContent = "Motion on";
  toggle.setAttribute("aria-pressed", "true");
  button.after(toggle);
  let animation = 0,
    visible = true;
  const reduced = matchMedia("(prefers-reduced-motion: reduce)"),
    mesh = crownMesh();
  const stop = () => {
    cancelAnimationFrame(animation);
    animation = 0;
    canvas.hidden = true;
    img.hidden = false;
    button.dataset.animating = "false";
  };
  toggle.onclick = () => {
    motionAllowed = !motionAllowed;
    toggle.textContent = motionAllowed ? "Motion on" : "Motion off";
    toggle.setAttribute("aria-pressed", String(motionAllowed));
    stop();
  };
  const context = canvas.getContext("2d");
  if (!context) {
    button.title = "MemoryKing static crown";
    return;
  }
  const draw = (angle: number) => {
    const ratio = Math.min(devicePixelRatio, 2);
    canvas.width = 160 * ratio;
    canvas.height = 160 * ratio;
    context.scale(ratio, ratio);
    context.clearRect(0, 0, 160, 160);
    const rotate = ([x, y, z]: Point): Point => {
      const a = x * Math.cos(angle) + z * Math.sin(angle),
        b = -x * Math.sin(angle) + z * Math.cos(angle);
      return [
        a,
        y * Math.cos(-0.18) - b * Math.sin(-0.18),
        y * Math.sin(-0.18) + b * Math.cos(-0.18),
      ];
    };
    const faces = mesh
      .map((f) => ({ color: f.color, points: f.points.map(rotate) }))
      .sort(
        (a, b) =>
          a.points.reduce((s, p) => s + p[2], 0) / a.points.length -
          b.points.reduce((s, p) => s + p[2], 0) / b.points.length,
      );
    for (const face of faces) {
      context.beginPath();
      face.points.forEach(([x, y, z], i) => {
        const perspective = 230 / (230 - z);
        const px = 80 + x * perspective,
          py = 85 + y * perspective;
        i ? context.lineTo(px, py) : context.moveTo(px, py);
      });
      context.closePath();
      context.fillStyle = face.color;
      context.fill();
      context.strokeStyle = "#ffffff22";
      context.lineWidth = 0.5;
      context.stroke();
    }
  };
  button.onclick = () => {
    stop();
    if (!motionAllowed || reduced.matches || document.hidden || !visible)
      return;
    frames = 0;
    const start = performance.now();
    canvas.hidden = false;
    img.hidden = true;
    button.dataset.animating = "true";
    const frame = (at: number) => {
      frames++;
      button.dataset.motionFrames = String(frames);
      button.dataset.motionElapsedMs = String(Math.round(at - start));
      const t = Math.min(1, (at - start) / 2400);
      if (document.hidden || !visible || reduced.matches || t >= 1) {
        stop();
        return;
      }
      draw(-0.45 + Math.PI * 2 * (1 - (1 - t) ** 3));
      animation = requestAnimationFrame(frame);
    };
    animation = requestAnimationFrame(frame);
  };
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stop();
  });
  reduced.addEventListener("change", stop);
  new IntersectionObserver((entries) => {
    visible = entries[0]?.isIntersecting ?? false;
    if (!visible) stop();
  }).observe(button);
}
