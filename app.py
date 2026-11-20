_HTML_HOME = """<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BetPulse Pro — Chargement…</title>
<style>
  :root {
    --bg: #0f172a;
    --text: #e2e8f0;
    --muted: #94a3b8;
    --accent: #38bdf8;
    --green: #10b981;
  }
  * { box-sizing: border-box; }
  html, body {
    margin: 0; padding: 0; height: 100vh; overflow: hidden;
    font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
    background: radial-gradient(circle at 50% 30%, #1e293b 0%, var(--bg) 70%);
    color: var(--text);
  }
  .field {
    position: fixed; inset: 0; overflow: hidden;
    background-image:
      linear-gradient(rgba(56,189,248,.04) 1px, transparent 1px),
      linear-gradient(90deg, rgba(56,189,248,.04) 1px, transparent 1px);
    background-size: 60px 60px;
  }
  .ball {
    position: absolute; top: 0; left: 0;
    will-change: transform;
    filter: drop-shadow(0 6px 14px rgba(0,0,0,.45));
  }
  .football {
    width: 64px; height: 64px; border-radius: 50%;
    background:
      radial-gradient(circle at 50% 50%, #111 17%, transparent 19%),
      radial-gradient(circle at 22% 22%, #111 11%, transparent 13%),
      radial-gradient(circle at 78% 22%, #111 11%, transparent 13%),
      radial-gradient(circle at 15% 55%, #111 11%, transparent 13%),
      radial-gradient(circle at 85% 55%, #111 11%, transparent 13%),
      radial-gradient(circle at 30% 85%, #111 11%, transparent 13%),
      radial-gradient(circle at 70% 85%, #111 11%, transparent 13%),
      radial-gradient(circle at 40% 40%, #fff 0%, #eee 55%, #b0b0b0 100%);
    box-shadow: inset -6px -6px 14px rgba(0,0,0,.28), inset 4px 4px 8px rgba(255,255,255,.4);
  }
  .tennis {
    width: 54px; height: 54px;
  }
  .tennis svg {
    width: 100%; height: 100%;
  }
  .center-card {
    position: absolute; top: 50%; left: 50%;
    transform: translate(-50%, -50%);
    z-index: 10; text-align: center;
    padding: 32px 40px; border-radius: 20px;
    background: rgba(30, 41, 59, 0.82);
    border: 1px solid rgba(56,189,248,.25);
    backdrop-filter: blur(10px);
    -webkit-backdrop-filter: blur(10px);
    box-shadow: 0 20px 50px rgba(0,0,0,.5);
    max-width: 440px;
    animation: fadeIn .5s ease-out;
  }
  @keyframes fadeIn {
    from { opacity: 0; transform: translate(-50%, -45%); }
    to   { opacity: 1; transform: translate(-50%, -50%); }
  }
  .logo-badge {
    display: inline-flex; align-items: center; justify-content: center;
    width: 74px; height: 74px; border-radius: 20px;
    background: linear-gradient(135deg, #38bdf8 0%, #0284c7 100%);
    color: #0f172a; font-weight: 900; font-size: 1.7rem;
    margin-bottom: 18px;
    animation: pulse 2.2s cubic-bezier(.4,0,.6,1) infinite;
  }
  @keyframes pulse {
    0%   { box-shadow: 0 0 0 0 rgba(56,189,248,.6); }
    70%  { box-shadow: 0 0 0 22px rgba(56,189,248,0); }
    100% { box-shadow: 0 0 0 0 rgba(56,189,248,0); }
  }
  .title { font-size: 1.9rem; font-weight: 800; margin: 0 0 6px; }
  .title span { color: var(--accent); }
  .tagline { color: var(--muted); font-size: .9rem; margin-bottom: 22px; }
  .progress {
    height: 6px; border-radius: 999px; overflow: hidden;
    background: rgba(148,163,184,.2); margin-bottom: 14px;
  }
  .progress-fill {
    height: 100%; width: 35%; border-radius: 999px;
    background: linear-gradient(90deg, var(--accent), var(--green));
    animation: slide 1.8s ease-in-out infinite;
    box-shadow: 0 0 12px rgba(56,189,248,.85);
  }
  @keyframes slide {
    0%   { transform: translateX(-110%); }
    100% { transform: translateX(380%); }
  }
  .status { font-size: .85rem; color: var(--muted); }
  .status::after {
    content: ''; display: inline-block; width: 1.3em;
    animation: dots 1.5s steps(4,end) infinite;
  }
  @keyframes dots {
    0% { content: ''; }
    25% { content: '.'; }
    50% { content: '..'; }
    75%, 100% { content: '...'; }
  }
  .meta { margin-top: 10px; font-size: .75rem; color: #64748b; }

  /* Trajectoires de rebond des ballons */
  @keyframes fly1 {
    0%   { transform: translate(8vw, 18vh) rotate(0deg); }
    20%  { transform: translate(25vw, 72vh) rotate(180deg); }
    40%  { transform: translate(60vw, 15vh) rotate(360deg); }
    60%  { transform: translate(85vw, 70vh) rotate(540deg); }
    80%  { transform: translate(45vw, 85vh) rotate(720deg); }
    100% { transform: translate(8vw, 18vh) rotate(900deg); }
  }
  @keyframes fly2 {
    0%   { transform: translate(88vw, 12vh) rotate(0deg); }
    25%  { transform: translate(60vw, 78vh) rotate(180deg); }
    50%  { transform: translate(20vw, 35vh) rotate(360deg); }
    75%  { transform: translate(70vw, 60vh) rotate(540deg); }
    100% { transform: translate(88vw, 12vh) rotate(720deg); }
  }
  @keyframes fly3 {
    0%   { transform: translate(50vw, 88vh) rotate(0deg); }
    20%  { transform: translate(80vw, 30vh) rotate(144deg); }
    40%  { transform: translate(30vw, 10vh) rotate(288deg); }
    60%  { transform: translate(10vw, 60vh) rotate(432deg); }
    80%  { transform: translate(70vw, 80vh) rotate(576deg); }
    100% { transform: translate(50vw, 88vh) rotate(720deg); }
  }
  @keyframes fly4 {
    0%   { transform: translate(15vw, 55vh) rotate(0deg); }
    25%  { transform: translate(50vw, 20vh) rotate(180deg); }
    50%  { transform: translate(90vw, 65vh) rotate(360deg); }
    75%  { transform: translate(40vw, 80vh) rotate(540deg); }
    100% { transform: translate(15vw, 55vh) rotate(720deg); }
  }
  @keyframes fly5 {
    0%   { transform: translate(70vw, 15vh) rotate(0deg); }
    20%  { transform: translate(20vw, 45vh) rotate(180deg); }
    40%  { transform: translate(60vw, 80vh) rotate(360deg); }
    60%  { transform: translate(90vw, 35vh) rotate(540deg); }
    80%  { transform: translate(30vw, 70vh) rotate(720deg); }
    100% { transform: translate(70vw, 15vh) rotate(900deg); }
  }

  @media (max-width: 480px) {
    .football { width: 48px; height: 48px; }
    .tennis { width: 42px; height: 42px; }
    .center-card { padding: 26px 22px; max-width: 92vw; }
    .title { font-size: 1.6rem; }
  }
</style>
</head>
<body>
<div class="field">
  <!-- Ballons de foot -->
  <div class="ball football" style="animation: fly1 11s linear infinite; animation-delay: 0s;"></div>
  <div class="ball football" style="animation: fly3 13s ease-in-out infinite; animation-delay: -4s;"></div>
  <div class="ball football" style="animation: fly5 15s linear infinite; animation-delay: -8s;"></div>

  <!-- Balles de tennis -->
  <div class="ball tennis" style="animation: fly2 10s ease-in-out infinite; animation-delay: -2s;">
    <svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">
      <defs>
        <radialGradient id="tg1" cx="35%" cy="35%">
          <stop offset="0%" stop-color="#f0f56a"/>
          <stop offset="100%" stop-color="#b8c92b"/>
        </radialGradient>
      </defs>
      <circle cx="50" cy="50" r="48" fill="url(#tg1)"/>
      <path d="M 12,14 Q 50,50 12,86" fill="none" stroke="#fff" stroke-width="2.5" stroke-linecap="round"/>
      <path d="M 88,14 Q 50,50 88,86" fill="none" stroke="#fff" stroke-width="2.5" stroke-linecap="round"/>
    </svg>
  </div>
  <div class="ball tennis" style="animation: fly4 12s linear infinite; animation-delay: -6s;">
    <svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">
      <defs>
        <radialGradient id="tg2" cx="35%" cy="35%">
          <stop offset="0%" stop-color="#f0f56a"/>
          <stop offset="100%" stop-color="#b8c92b"/>
        </radialGradient>
      </defs>
      <circle cx="50" cy="50" r="48" fill="url(#tg2)"/>
      <path d="M 12,14 Q 50,50 12,86" fill="none" stroke="#fff" stroke-width="2.5" stroke-linecap="round"/>
      <path d="M 88,14 Q 50,50 88,86" fill="none" stroke="#fff" stroke-width="2.5" stroke-linecap="round"/>
    </svg>
  </div>
</div>

<div class="center-card">
  <div class="logo-badge">BP</div>
  <h1 class="title">BetPulse <span>Pro</span></h1>
  <div class="tagline">Préparation des pronostics</div>
  <div class="progress"><div class="progress-fill"></div></div>
  <div class="status" id="status">
    {% if en_cours %}Analyse en cours{% else %}Initialisation des données{% endif %}
  </div>
  <div class="meta" id="meta"></div>
</div>

<script>
(function(){
  var statusEl = document.getElementById('status');
  var metaEl   = document.getElementById('meta');
  var POLL_MS  = 1500;

  async function check() {
    try {
      var r = await fetch("/api/status", { cache: "no-store" });
      if (r.ok) {
        var d = await r.json();
        if (d.ready) {
          statusEl.textContent = "Prêt ! Ouverture de l'application";
          setTimeout(function(){ window.location.replace("/app"); }, 200);
          return;
        }
        if (d.mis_a_jour) metaEl.textContent = "Dernière analyse : " + d.mis_a_jour;
      }
    } catch (e) { /* blip réseau, on retente */ }
    setTimeout(check, POLL_MS);
  }
  setTimeout(check, 300);
})();
</script>
</body>
</html>
"""
