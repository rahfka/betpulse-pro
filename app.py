_HTML_HOME = """<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>BetPulse Pro — Chargement en direct</title>
  <style>
    :root {
      --bg: #070d19;
      --card: rgba(19, 29, 49, 0.78);
      --text: #e2e8f0;
      --muted: #94a3b8;
      --accent: #38bdf8;
      --accent2: #10b981;
      --border: rgba(56, 189, 248, 0.25);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      overflow: hidden;
      font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
      background: radial-gradient(circle at 20% 20%, #13294b 0%, var(--bg) 60%);
      color: var(--text);
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 24px;
      position: relative;
    }
    #bg-canvas {
      position: fixed;
      inset: 0;
      width: 100%;
      height: 100%;
      z-index: 0;
      pointer-events: none;
    }
    .pulse-ring {
      position: fixed;
      width: 420px;
      height: 420px;
      border-radius: 50%;
      border: 1px solid rgba(56, 189, 248, 0.16);
      box-shadow: 0 0 80px rgba(56, 189, 248, 0.08) inset;
      animation: breathe 4.5s ease-in-out infinite;
      z-index: 0;
      pointer-events: none;
    }
    @keyframes breathe {
      0%, 100% { transform: scale(0.92); opacity: 0.45; }
      50% { transform: scale(1.12); opacity: 0.9; }
    }
    .stage {
      position: relative;
      z-index: 1;
      width: 100%;
      max-width: 460px;
      background: var(--card);
      backdrop-filter: blur(12px);
      border: 1px solid var(--border);
      border-radius: 26px;
      padding: 36px 28px;
      text-align: center;
      box-shadow: 0 24px 60px rgba(0, 0, 0, 0.5);
    }
    .orbit-box {
      position: relative;
      width: 108px;
      height: 108px;
      margin: 0 auto 20px auto;
      display: flex;
      align-items: center;
      justify-content: center;
    }
    .orbit-ring {
      position: absolute;
      inset: 0;
      border-radius: 50%;
      border: 3px solid transparent;
      border-top-color: var(--accent);
      border-right-color: var(--accent2);
      animation: spin 1.35s linear infinite;
    }
    .orbit-ring-inner {
      position: absolute;
      inset: 10px;
      border-radius: 50%;
      border: 2px dashed rgba(56, 189, 248, 0.35);
      animation: spinReverse 3.2s linear infinite;
    }
    @keyframes spin { to { transform: rotate(360deg); } }
    @keyframes spinReverse { to { transform: rotate(-360deg); } }
    .core-mark {
      width: 66px;
      height: 66px;
      border-radius: 18px;
      background: linear-gradient(135deg, #38bdf8 0%, #0284c7 100%);
      color: #070d19;
      font-weight: 900;
      font-size: 1.5rem;
      display: flex;
      align-items: center;
      justify-content: center;
      box-shadow: 0 0 24px rgba(56, 189, 248, 0.45);
    }
    h1 { margin: 0 0 8px 0; font-size: 1.85rem; font-weight: 800; }
    h1 span { color: var(--accent); }
    .status-text {
      color: #bae6fd;
      font-size: 0.95rem;
      font-weight: 600;
      min-height: 24px;
      margin-bottom: 14px;
    }
    .progress-bar {
      width: 100%;
      height: 6px;
      background: rgba(15, 23, 42, 0.9);
      border-radius: 999px;
      overflow: hidden;
      margin-bottom: 16px;
    }
    .progress-fill {
      height: 100%;
      width: 35%;
      border-radius: 999px;
      background: linear-gradient(90deg, var(--accent), var(--accent2));
      animation: slideBar 1.8s ease-in-out infinite;
    }
    @keyframes slideBar {
      0% { transform: translateX(-110%); width: 45%; }
      50% { width: 65%; }
      100% { transform: translateX(220%); width: 45%; }
    }
    .sub-meta { font-size: 0.82rem; color: var(--muted); }
    .direct-link {
      display: inline-block;
      margin-top: 18px;
      color: var(--accent);
      text-decoration: none;
      font-size: 0.85rem;
      font-weight: 700;
      opacity: 0.85;
    }
    .direct-link:hover { opacity: 1; text-decoration: underline; }
  </style>
</head>
<body>
  <canvas id="bg-canvas"></canvas>
  <div class="pulse-ring"></div>

  <div class="stage">
    <div class="orbit-box">
      <div class="orbit-ring"></div>
      <div class="orbit-ring-inner"></div>
      <div class="core-mark">BP</div>
    </div>
    <h1>BetPulse <span>Pro</span></h1>
    <div id="live-status" class="status-text">
      {% if r.mis_a_jour %}
        Données prêtes — ouverture de l'espace matchs…
      {% else %}
        Scan des compétitions Foot &amp; Tennis en cours…
      {% endif %}
    </div>
    <div class="progress-bar"><div class="progress-fill"></div></div>
    <div class="sub-meta" id="sub-meta">
      {% if r.mis_a_jour %}
        Dernière analyse : {{ r.mis_a_jour }}
      {% else %}
        Redirection automatique vers /app dès la fin du chargement
      {% endif %}
    </div>
    <a class="direct-link" href="/app">Aller directement à /app →</a>
  </div>

  <script>
    const canvas = document.getElementById("bg-canvas");
    const ctx = canvas.getContext("2d");
    let w, h, dots;

    function initCanvas() {
      w = canvas.width = window.innerWidth;
      h = canvas.height = window.innerHeight;
      const count = Math.min(55, Math.floor((w * h) / 24000));
      dots = Array.from({ length: count }, () => ({
        x: Math.random() * w,
        y: Math.random() * h,
        vx: (Math.random() - 0.5) * 0.7,
        vy: (Math.random() - 0.5) * 0.7,
        r: 1.5 + Math.random() * 2.2
      }));
    }

    function drawCanvas() {
      ctx.clearRect(0, 0, w, h);
      for (let i = 0; i < dots.length; i++) {
        const d = dots[i];
        d.x += d.vx;
        d.y += d.vy;
        if (d.x < 0 || d.x > w) d.vx *= -1;
        if (d.y < 0 || d.y > h) d.vy *= -1;

        ctx.beginPath();
        ctx.arc(d.x, d.y, d.r, 0, Math.PI * 2);
        ctx.fillStyle = "rgba(56, 189, 248, 0.55)";
        ctx.fill();

        for (let j = i + 1; j < dots.length; j++) {
          const d2 = dots[j];
          const dist = Math.hypot(d.x - d2.x, d.y - d2.y);
          if (dist < 130) {
            ctx.beginPath();
            ctx.moveTo(d.x, d.y);
            ctx.lineTo(d2.x, d2.y);
            ctx.strokeStyle = `rgba(56, 189, 248, ${0.14 * (1 - dist / 130)})`;
            ctx.lineWidth = 1;
            ctx.stroke();
          }
        }
      }
      requestAnimationFrame(drawCanvas);
    }

    window.addEventListener("resize", initCanvas);
    initCanvas();
    drawCanvas();

    const etapes = [
      "Récupération des calendriers Football…",
      "Calcul des probabilités Poisson & classements Tennis…",
      "Vérification des alertes blessures & forfaits…"
    ];
    let idxEtape = 0;
    const statusEl = document.getElementById("live-status");

    setInterval(() => {
      idxEtape = (idxEtape + 1) % etapes.length;
      if (statusEl && !statusEl.dataset.ready) {
        statusEl.textContent = etapes[idxEtape];
      }
    }, 2200);

    async function verifierEtRediriger() {
      try {
        const res = await fetch("/api/status", { cache: "no-store" });
        if (res.ok) {
          const data = await res.json();
          if (data.ready) {
            if (statusEl) {
              statusEl.dataset.ready = "1";
              statusEl.textContent = "Analyse terminée — redirection vers /app…";
            }
            window.location.replace("/app");
            return;
          }
        }
      } catch (e) {}
      setTimeout(verifierEtRediriger, 1500);
    }

    verifierEtRediriger();
  </script>
</body>
</html>
"""
