/* مجرّة الأقسام: جسيمات تتفاعل مع الفأرة + أقسام تدور حول مركز الصفحة */
(function () {
  var stage = document.querySelector('[data-ex-stage]');
  if (!stage) return;
  var d = document.querySelector('[data-ex-date]');
  if (d) { try { d.textContent = new Date().toLocaleDateString('ar-EG', { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' }); } catch (e) {} }

  var cv = stage.querySelector('[data-ex-canvas]'), ctx = cv.getContext('2d');
  var nodes = [].slice.call(stage.querySelectorAll('[data-ex-node]'));
  var reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var W = 0, H = 0, dpr = Math.min(window.devicePixelRatio || 1, 2);
  var mouse = { x: 0, y: 0, tx: 0, ty: 0, px: 0, py: 0, in: false };
  var parts = [], angle = 0, speed = 0.0016, hovered = false, last = 0;
  var colors = ['#F2C811', '#111111', '#F2C811', '#111111', '#D9A900'];

  function narrow() { return W < 760; }
  function resize() {
    var r = stage.getBoundingClientRect(); W = r.width; H = r.height;
    cv.width = W * dpr; cv.height = H * dpr; ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    var n = Math.round(Math.min(90, W * H / 9000));
    parts = [];
    for (var i = 0; i < n; i++) parts.push({ x: Math.random() * W, y: Math.random() * H, vx: (Math.random() - .5) * .3, vy: (Math.random() - .5) * .3, r: Math.random() * 1.8 + .6, c: colors[i % colors.length] });
  }

  function place() {
    if (narrow()) { nodes.forEach(function (n) { n.style.transform = ''; }); return; }
    var cx = W / 2 + mouse.x * -18, cy = H / 2 + mouse.y * -14;
    var rx = Math.min(W / 2 - 125, Math.max(160, W * 0.37)), ry = Math.min(H / 2 - 62, Math.max(120, H * 0.34)), n = nodes.length;
    nodes.forEach(function (el, i) {
      var a = angle + i * Math.PI * 2 / n - Math.PI / 2;
      var depth = (Math.sin(a) + 1) / 2;
      var s = 0.82 + depth * 0.26;
      var x = cx + Math.cos(a) * rx + mouse.x * (depth - .5) * 40;
      var y = cy + Math.sin(a) * ry + mouse.y * (depth - .5) * 30;
      el.style.transform = 'translate(' + x.toFixed(1) + 'px,' + y.toFixed(1) + 'px) scale(' + s.toFixed(3) + ')';
      el.style.zIndex = Math.round(depth * 10);
      el.style.opacity = (0.65 + depth * 0.35).toFixed(2);
      el._p = { x: x, y: y, c: getComputedStyle(el).getPropertyValue('--c').trim() || '#4D8DFF' };
    });
  }

  function draw() {
    ctx.clearRect(0, 0, W, H);
    var i, j, p, q, dx, dy, dist;
    for (i = 0; i < parts.length; i++) {
      p = parts[i];
      if (mouse.in) {
        dx = p.x - mouse.px; dy = p.y - mouse.py; dist = Math.sqrt(dx * dx + dy * dy);
        if (dist < 120 && dist > 0) { p.x += dx / dist * 1.6; p.y += dy / dist * 1.6; }
      }
      p.x += p.vx; p.y += p.vy;
      if (p.x < 0 || p.x > W) p.vx *= -1;
      if (p.y < 0 || p.y > H) p.vy *= -1;
      for (j = i + 1; j < parts.length; j++) {
        q = parts[j]; dx = p.x - q.x; dy = p.y - q.y; dist = dx * dx + dy * dy;
        if (dist < 9000) {
          ctx.strokeStyle = 'rgba(20,20,20,' + (0.22 * (1 - dist / 9000)).toFixed(3) + ')';
          ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(p.x, p.y); ctx.lineTo(q.x, q.y); ctx.stroke();
        }
      }
      ctx.fillStyle = p.c; ctx.globalAlpha = .8; ctx.beginPath(); ctx.arc(p.x, p.y, p.r, 0, 6.283); ctx.fill(); ctx.globalAlpha = 1;
    }
    if (!narrow()) {
      var cx = W / 2 + mouse.x * -18, cy = H / 2 + mouse.y * -14;
      ctx.strokeStyle = 'rgba(20,20,20,.14)'; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.ellipse(cx, cy, Math.min(W / 2 - 125, Math.max(160, W * 0.37)), Math.min(H / 2 - 62, Math.max(120, H * 0.34)), 0, 0, 6.283); ctx.stroke();
      nodes.forEach(function (el) {
        if (!el._p) return;
        var g = ctx.createLinearGradient(cx, cy, el._p.x, el._p.y);
        g.addColorStop(0, 'rgba(242,200,17,0)'); g.addColorStop(1, '#111111');
        ctx.globalAlpha = (el === document.activeElement || el.matches(':hover')) ? .9 : .28;
        ctx.strokeStyle = g; ctx.lineWidth = 1.4; ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(el._p.x, el._p.y); ctx.stroke(); ctx.globalAlpha = 1;
      });
    }
  }

  function frame(t) {
    var dt = Math.min(48, t - last); last = t;
    mouse.x += (mouse.tx - mouse.x) * .06; mouse.y += (mouse.ty - mouse.y) * .06;
    if (!hovered && !reduce) angle += speed * dt / 16;
    place(); draw();
    if (!document.hidden) requestAnimationFrame(frame);
    else document.addEventListener('visibilitychange', resume, { once: true });
  }
  function resume() { last = performance.now(); requestAnimationFrame(frame); }

  stage.addEventListener('mousemove', function (e) {
    var r = stage.getBoundingClientRect();
    mouse.px = e.clientX - r.left; mouse.py = e.clientY - r.top; mouse.in = true;
    mouse.tx = (mouse.px / r.width - .5) * 2; mouse.ty = (mouse.py / r.height - .5) * 2;
  });
  stage.addEventListener('mouseleave', function () { mouse.in = false; mouse.tx = mouse.ty = 0; });
  nodes.forEach(function (n) {
    n.addEventListener('mouseenter', function () { hovered = true; });
    n.addEventListener('mouseleave', function () { hovered = false; });
    n.addEventListener('focus', function () { hovered = true; });
    n.addEventListener('blur', function () { hovered = false; });
  });
  window.addEventListener('resize', resize);
  resize();
  if (reduce) { place(); draw(); } else requestAnimationFrame(frame);
})();
