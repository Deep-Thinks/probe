/* /static/coin-particles.js — Probe 共用粒子库（零依赖、单文件）
 *
 * 暴露 window.ProbeFX：
 *   - makeCoinSystem(canvas)  局部 canvas 上的金币粒子（爆金币用）
 *   - spawnBills({x,y,count,spread,power})  全屏 layer 上的钞票
 *   - spawnConfetti({x,y,count,spread})     全屏 layer 上的撒花
 *   - triggerJackpot(shake?)                撒花 + 可选震屏
 *   - billsCountForUsd(usd)                 钞票数量公式
 *
 * 全屏 scene-layer 自动按需创建并贴在 <body>，pointer-events:none，z-index:9999。
 * 遵守 prefers-reduced-motion：用户偏好减弱动效时，全部 spawn 操作 no-op。
 */
(function () {
  'use strict';
  if (window.ProbeFX) return;

  var DPR = window.devicePixelRatio || 1;
  var REDUCED = !!(window.matchMedia &&
                   window.matchMedia('(prefers-reduced-motion: reduce)').matches);

  /* ==================================================
     全屏 scene-layer：钞票 + 撒花共用，按需创建
     ================================================== */
  var sceneLayer = null;
  var sctx = null;
  var sceneParticles = [];
  var sceneRaf = null;
  var sceneLastT = 0;

  function ensureSceneLayer() {
    if (sceneLayer) return;
    sceneLayer = document.createElement('canvas');
    sceneLayer.className = 'scene-layer';
    sceneLayer.setAttribute('aria-hidden', 'true');
    document.body.appendChild(sceneLayer);
    sctx = sceneLayer.getContext('2d');
    resizeScene();
    window.addEventListener('resize', resizeScene);
  }
  function resizeScene() {
    if (!sceneLayer) return;
    sceneLayer.width = window.innerWidth * DPR;
    sceneLayer.height = window.innerHeight * DPR;
    sctx.setTransform(DPR, 0, 0, DPR, 0, 0);
  }
  function pushScene(p) {
    ensureSceneLayer();
    sceneParticles.push(p);
    if (!sceneRaf) sceneRaf = requestAnimationFrame(sceneLoop);
  }
  function sceneLoop(t) {
    var dt = sceneLastT ? Math.min(t - sceneLastT, 33) : 16;
    sceneLastT = t;
    sctx.clearRect(0, 0, window.innerWidth, window.innerHeight);
    var alive = 0;
    for (var i = 0; i < sceneParticles.length; i++) {
      var p = sceneParticles[i];
      if (p.life >= p.maxLife) continue;
      p.life += dt;
      p.vy += p.gravity;
      p.vx *= 0.998;
      p.x += p.vx;
      p.y += p.vy;
      p.rot += p.rotSpeed;
      if (p.y > window.innerHeight + 60) { p.life = p.maxLife; continue; }
      var fadeStart = p.maxLife * 0.7;
      var alpha = p.life < fadeStart
                  ? 1
                  : 1 - (p.life - fadeStart) / (p.maxLife - fadeStart);
      if (alpha < 0) alpha = 0;
      sctx.save();
      sctx.globalAlpha = alpha;
      sctx.translate(p.x, p.y);
      sctx.rotate(p.rot);
      if (p.kind === 'bill') drawBill(sctx, p);
      else if (p.kind === 'confetti') drawConfetti(sctx, p);
      sctx.restore();
      alive++;
    }
    if (alive > 0) {
      sceneRaf = requestAnimationFrame(sceneLoop);
    } else {
      sceneRaf = null;
      sceneLastT = 0;
      sceneParticles = [];
      sctx.clearRect(0, 0, window.innerWidth, window.innerHeight);
    }
  }

  /* 钞票绘制：墨绿矩形 + 中央米色徽记 + rotate 翻转效果 */
  function drawBill(ctx, p) {
    var sx = Math.abs(Math.cos(p.rot * 1.2));
    if (sx < 0.18) sx = 0.18;
    ctx.scale(sx, 1);
    ctx.fillStyle = '#2F6B4F';
    ctx.fillRect(-p.w / 2, -p.h / 2, p.w, p.h);
    ctx.strokeStyle = '#1B231E';
    ctx.lineWidth = 0.8;
    ctx.strokeRect(-p.w / 2, -p.h / 2, p.w, p.h);
    ctx.fillStyle = '#E3EDE6';
    ctx.beginPath();
    ctx.arc(0, 0, p.h * 0.28, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = 'rgba(227,237,230,0.65)';
    ctx.fillRect(-p.w / 2 + 1.5, -p.h / 2 + 1, 2.5, p.h - 2);
    ctx.fillRect(p.w / 2 - 4, -p.h / 2 + 1, 2.5, p.h - 2);
  }

  /* 撒花配色：项目语义色系（金 + 墨绿 + 米 + 朱砂 + 墨） */
  var CONF_COLORS = ['#9A6B12', '#1E4E40', '#F3E9D2', '#A8412A', '#20251F'];
  function drawConfetti(ctx, p) {
    ctx.fillStyle = p.color;
    if (p.shape === 'rect') {
      ctx.fillRect(-p.w / 2, -p.h / 2, p.w, p.h);
    } else {
      ctx.beginPath();
      ctx.arc(0, 0, p.w / 2, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  function spawnBills(opts) {
    if (REDUCED) return;
    var n = opts.count || 5;
    var ox = opts.x, oy = opts.y;
    var spread = (opts.spread || 110) * Math.PI / 180;
    var center = -Math.PI / 2;
    var power = opts.power || 1;
    for (var i = 0; i < n; i++) {
      var ang = center + (Math.random() - 0.5) * spread;
      var speed = (4 + Math.random() * 6) * power;
      pushScene({
        kind: 'bill',
        x: ox + (Math.random() - 0.5) * 24,
        y: oy + (Math.random() - 0.5) * 8,
        vx: Math.cos(ang) * speed,
        vy: Math.sin(ang) * speed,
        w: 18 + Math.random() * 4,
        h: 11 + Math.random() * 2,
        rot: (Math.random() - 0.5) * 0.6,
        rotSpeed: (Math.random() - 0.5) * 0.35,
        gravity: 0.18,
        life: 0,
        maxLife: 1800 + Math.random() * 900
      });
    }
  }

  function spawnConfetti(opts) {
    if (REDUCED) return;
    opts = opts || {};
    var n = opts.count || 120;
    var ox = opts.x != null ? opts.x : window.innerWidth / 2;
    var oy = opts.y != null ? opts.y : window.innerHeight * 0.85;
    var spread = (opts.spread || 70) * Math.PI / 180;
    var center = -Math.PI / 2;
    for (var i = 0; i < n; i++) {
      var ang = center + (Math.random() - 0.5) * spread;
      var speed = 9 + Math.random() * 10;
      pushScene({
        kind: 'confetti',
        x: ox + (Math.random() - 0.5) * 30,
        y: oy,
        vx: Math.cos(ang) * speed,
        vy: Math.sin(ang) * speed,
        w: 6 + Math.random() * 5,
        h: 4 + Math.random() * 3,
        rot: Math.random() * Math.PI * 2,
        rotSpeed: (Math.random() - 0.5) * 0.5,
        gravity: 0.22,
        life: 0,
        maxLife: 1800 + Math.random() * 900,
        color: CONF_COLORS[i % CONF_COLORS.length],
        shape: Math.random() < 0.5 ? 'rect' : 'circle'
      });
    }
  }

  function triggerJackpot(shake) {
    if (REDUCED) return;
    if (shake) {
      document.body.classList.remove('screen-shake');
      void document.body.offsetWidth;
      document.body.classList.add('screen-shake');
      setTimeout(function () {
        document.body.classList.remove('screen-shake');
      }, 700);
    }
    spawnConfetti({
      count: 120, spread: 60,
      x: window.innerWidth * 0.25,
      y: window.innerHeight * 0.9
    });
    spawnConfetti({
      count: 120, spread: 60,
      x: window.innerWidth * 0.75,
      y: window.innerHeight * 0.9
    });
  }

  /* 钞票数量公式：clamp(3, 60, floor(usd * 5))
     $0.38 → 3 · $1.20 → 6 · $3.45 → 17 · $17.50 → 60 */
  function billsCountForUsd(usd) {
    if (!isFinite(usd) || usd <= 0) return 0;
    return Math.min(60, Math.max(3, Math.floor(usd * 5)));
  }

  /* ==================================================
     局部金币粒子（爆金币）
     ================================================== */
  function makeCoinSystem(canvas) {
    var ctx = canvas.getContext('2d');
    var raf = null;
    var lastT = 0;
    var particles = [];

    function resize() {
      canvas.width = canvas.clientWidth * DPR;
      canvas.height = canvas.clientHeight * DPR;
      ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    }
    function W() { return canvas.clientWidth; }
    function H() { return canvas.clientHeight; }
    resize();

    function spawn(opts) {
      if (REDUCED) return;
      var n = opts.n || 5;
      for (var i = 0; i < n; i++) {
        particles.push({
          x: opts.x + (Math.random() - 0.5) * (opts.spawnSpread || 30),
          y: opts.y,
          vx: (Math.random() - 0.5) * (opts.vxRange || 7),
          vy: -(opts.vyMin || 4) -
              Math.random() * ((opts.vyMax || 8) - (opts.vyMin || 4)),
          r: (opts.radius || 12) + (Math.random() - 0.5) * 3,
          rot: Math.random() * Math.PI * 2,
          rotSpeed: (Math.random() - 0.5) * 0.35,
          life: 0,
          maxLife: opts.life || 1500,
          gravity: opts.gravity != null ? opts.gravity : 0.22
        });
      }
      if (!raf) raf = requestAnimationFrame(loop);
    }

    function loop(t) {
      var dt = lastT ? Math.min(t - lastT, 33) : 16;
      lastT = t;
      ctx.clearRect(0, 0, W(), H());
      var alive = 0;
      for (var i = 0; i < particles.length; i++) {
        var p = particles[i];
        if (p.life >= p.maxLife) continue;
        p.life += dt;
        p.vy += p.gravity;
        p.x += p.vx;
        p.y += p.vy;
        p.rot += p.rotSpeed;
        var alpha = 1 - p.life / p.maxLife;
        if (alpha < 0) alpha = 0;
        ctx.save();
        ctx.globalAlpha = alpha;
        ctx.translate(p.x, p.y);
        ctx.rotate(p.rot);
        var rx = p.r * Math.abs(Math.cos(p.rot * 1.5));
        if (rx < 2) rx = 2;
        ctx.beginPath();
        ctx.ellipse(0, 0, rx, p.r, 0, 0, Math.PI * 2);
        ctx.fillStyle = '#9A6B12';
        ctx.fill();
        ctx.strokeStyle = '#20251F';
        ctx.lineWidth = 1;
        ctx.stroke();
        ctx.restore();
        alive++;
      }
      if (alive > 0) {
        raf = requestAnimationFrame(loop);
      } else {
        raf = null;
        lastT = 0;
        particles = [];
        ctx.clearRect(0, 0, W(), H());
      }
    }

    return { spawn: spawn, resize: resize, getW: W, getH: H };
  }

  window.ProbeFX = {
    makeCoinSystem: makeCoinSystem,
    spawnBills: spawnBills,
    spawnConfetti: spawnConfetti,
    triggerJackpot: triggerJackpot,
    billsCountForUsd: billsCountForUsd,
    isReducedMotion: function () { return REDUCED; }
  };
})();
