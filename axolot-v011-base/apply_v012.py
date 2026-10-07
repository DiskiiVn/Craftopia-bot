from pathlib import Path

root = Path('axolot-v011-base/source')

for rel in ['package.json','src-tauri/Cargo.toml','src-tauri/tauri.conf.json']:
    p = root / rel
    s = p.read_text(encoding='utf-8')
    s = s.replace('0.11.2','0.12.0').replace('Release Candidate 0.11.2','Release Candidate 0.12')
    p.write_text(s, encoding='utf-8')

p = root / 'src/main.tsx'
s = p.read_text(encoding='utf-8')
s = s.replace('CLIENT · NEBULA 0.11','CLIENT · NEBULA 0.12')
s = s.replace('AXOLOT NEBULA 0.11 // RELEASE LAYER','AXOLOT NEBULA 0.12 // MOTION CORE')
s = s.replace('AXOLOT NEBULA 0.11</span>','AXOLOT NEBULA 0.12</span>')

needle = "type RemoteConfig = { microsoft_client_id: string; update_manifest_url: string; latest_launcher_version: string; curseforge_enabled: boolean; curseforge_proxy_url: string; updated_at?: string | null };"
if 'type HealthItem' not in s:
    s = s.replace(needle, needle + "\ntype HealthState = 'ok' | 'warn' | 'error' | 'checking';\ntype HealthItem = { id: string; label: string; state: HealthState; detail: string };")

needle = "  const [crashReport, setCrashReport] = React.useState<CrashReportData | null>(null);\n  const consoleEndRef = React.useRef<HTMLDivElement | null>(null);"
if 'const [healthBusy' not in s:
    s = s.replace(needle, """  const [crashReport, setCrashReport] = React.useState<CrashReportData | null>(null);
  const [healthBusy, setHealthBusy] = React.useState(false);
  const [healthItems, setHealthItems] = React.useState<HealthItem[]>([
    { id: 'cloud', label: 'Axolot Cloud', state: 'checking', detail: 'Not checked yet' },
    { id: 'engine', label: 'Game engine', state: 'checking', detail: 'Not checked yet' },
    { id: 'updates', label: 'Update channel', state: 'checking', detail: 'Not checked yet' },
    { id: 'modrinth', label: 'Modrinth', state: 'checking', detail: 'Not checked yet' },
    { id: 'curseforge', label: 'CurseForge', state: 'checking', detail: 'Not checked yet' },
  ]);
  const consoleEndRef = React.useRef<HTMLDivElement | null>(null);""")

if 'const runSystemHealth' not in s:
    marker = '  const filteredProfiles = profiles.filter((profile) => {'
    health_fn = r'''  const runSystemHealth = async () => {
    if (healthBusy) return;
    setHealthBusy(true);
    const next: HealthItem[] = [];
    const push = (id: string, label: string, state: HealthState, detail: string) => next.push({ id, label, state, detail });

    let config = remoteConfig;
    try {
      const response = await fetch(AXOLOT_PUBLIC_CONFIG_URL, { cache: 'no-store' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      config = await response.json() as RemoteConfig;
      setRemoteConfig(config);
      setMicrosoftClientId((config.microsoft_client_id || '').trim());
      setCurseForgeEnabled(Boolean(config.curseforge_enabled));
      push('cloud', 'Axolot Cloud', 'ok', `Online · admin channel v${config.latest_launcher_version || 'unknown'}`);
    } catch (error) {
      push('cloud', 'Axolot Cloud', 'error', `Unavailable · ${String(error)}`);
    }

    try {
      if (!activeProfile) throw new Error('No active profile');
      const state = await invoke<GameProcessStatus>('get_game_status', { profileId: activeProfile.id });
      push('engine', 'Game engine', 'ok', `${state.state.toUpperCase()} · monitor responding`);
    } catch (error) {
      push('engine', 'Game engine', 'error', String(error));
    }

    try {
      const manifest = (config?.update_manifest_url || updateManifestUrl || DEFAULT_UPDATE_MANIFEST).trim();
      const update = await invoke<LauncherUpdateStatus>('check_launcher_update', { manifestUrl: manifest });
      setUpdateStatus(update);
      push('updates', 'Update channel', 'ok', update.available ? `v${update.latest_version} available` : `Current v${update.current_version}`);
    } catch (error) {
      push('updates', 'Update channel', 'warn', `Could not verify · ${String(error)}`);
    }

    try {
      if (!activeProfile) throw new Error('No active profile');
      const mods = await invoke<ModrinthProject[]>('search_modrinth', {
        query: 'sodium', projectType: 'mod', gameVersion: activeProfile.versionId, loader: activeProfile.loader,
      });
      push('modrinth', 'Modrinth', 'ok', `${mods.length} compatible result(s) returned`);
    } catch (error) {
      push('modrinth', 'Modrinth', 'error', String(error));
    }

    if (!config?.curseforge_enabled) {
      push('curseforge', 'CurseForge', 'warn', 'Admin API key not configured');
    } else {
      try {
        if (!activeProfile) throw new Error('No active profile');
        const mods = await invoke<ModrinthProject[]>('search_curseforge', {
          query: 'sodium', projectType: 'mod', gameVersion: activeProfile.versionId, loader: activeProfile.loader,
        });
        push('curseforge', 'CurseForge', 'ok', `${mods.length} compatible result(s) returned through Axolot Cloud`);
      } catch (error) {
        push('curseforge', 'CurseForge', 'error', String(error));
      }
    }

    setHealthItems(next);
    setHealthBusy(false);
    setStatus(next.every((item) => item.state === 'ok') ? 'System Health: all checked services are ready.' : 'System Health finished with one or more warnings.');
  };

'''
    if marker not in s:
        raise RuntimeError('filteredProfiles marker not found')
    s = s.replace(marker, health_fn + marker)

biolume = '''      <div className="biolume-field" aria-hidden="true">
        <span className="glow-orb orb-a" />
        <span className="glow-orb orb-b" />
        <span className="glow-orb orb-c" />
        <span className="bubble bubble-1" />
        <span className="bubble bubble-2" />
        <span className="bubble bubble-3" />
      </div>'''
if 'className="motion-field"' not in s:
    if biolume not in s: raise RuntimeError('biolume field marker not found')
    s = s.replace(biolume, biolume + '\n      <div className="motion-field" aria-hidden="true"><span/><span/><span/><span/><span/></div>')

release_tile = '''              <div className="setting-tile panel-glass cloud-managed"><span>Release channel</span><b>Axolot Stable</b><small>{remoteConfig?.latest_launcher_version ? `Admin channel reports v${remoteConfig.latest_launcher_version}` : 'Managed remotely by Axolot Cloud.'}</small></div>'''
health_ui = '''              <div className="setting-tile panel-glass health-tile"><div className="health-head"><div><span>SYSTEM HEALTH</span><b>Launcher diagnostics</b></div><button onClick={runSystemHealth} disabled={healthBusy}><Activity size={14}/>{healthBusy ? 'Checking…' : 'Run check'}</button></div><div className="health-list">{healthItems.map((item)=><div className={`health-row ${item.state}`} key={item.id}><i/><span><b>{item.label}</b><small>{item.detail}</small></span></div>)}</div><small>Checks Axolot Cloud, game process monitor, update channel and both mod providers without exposing private API keys.</small></div>'''
if 'Launcher diagnostics' not in s:
    if release_tile not in s: raise RuntimeError('release tile marker not found')
    s = s.replace(release_tile, release_tile + '\n' + health_ui)

boot_core = '''          <div className="boot-core">
            <div className="boot-axo"><span>A</span><i/><b/></div>
            <span className="micro-label">AXOLOT NEBULA 0.12</span>'''
if 'className="boot-atmosphere"' not in s:
    if boot_core not in s: raise RuntimeError('boot marker not found')
    s = s.replace(boot_core, '''          <div className="boot-atmosphere" aria-hidden="true"><i/><i/><i/><i/><i/><i/><i/><i/><i/><i/><i/><i/></div>
          <div className="boot-radar" aria-hidden="true"><span/><b/><i/></div>
''' + boot_core)

p.write_text(s, encoding='utf-8')

p = root / 'src/styles.css'
css = p.read_text(encoding='utf-8')
css = css.replace('.axo-brand:after{content:"0.11 RC"!important}', '.axo-brand:after{content:"0.12 RC"!important}')
css = css.replace('.hero-reef:after{content:"AXOLOT // NEBULA 0.11 // RELEASE LAYER"!important}', '.hero-reef:after{content:"AXOLOT // NEBULA 0.12 // MOTION CORE"!important}')
if 'AXOLOT v0.12 — MOTION CORE' not in css:
    css += r'''

/* ==============================
   AXOLOT v0.12 — MOTION CORE / PREMIUM POLISH
   ============================== */
.axo-app{isolation:isolate;overflow-x:hidden}
.axo-app:before{content:"";position:fixed;inset:-30%;z-index:-4;pointer-events:none;background:conic-gradient(from 90deg at 50% 50%,rgba(87,255,226,.025),rgba(122,119,255,.04),rgba(255,111,187,.025),rgba(87,255,226,.025));animation:axoAuroraSpin 34s linear infinite;filter:blur(80px)}
.motion-field{position:fixed;inset:0;z-index:-2;pointer-events:none;overflow:hidden;opacity:.52}
.motion-field span{position:absolute;width:1px;height:180px;background:linear-gradient(180deg,transparent,rgba(116,241,222,.26),transparent);filter:blur(.2px);transform:rotate(34deg);animation:axoDrift 12s linear infinite}
.motion-field span:nth-child(1){left:8%;top:-20%;animation-delay:-1s}.motion-field span:nth-child(2){left:31%;top:-32%;height:250px;animation-delay:-8s}.motion-field span:nth-child(3){left:57%;top:-16%;height:140px;animation-delay:-4s}.motion-field span:nth-child(4){left:77%;top:-28%;height:230px;animation-delay:-10s}.motion-field span:nth-child(5){left:93%;top:-12%;height:160px;animation-delay:-6s}
@keyframes axoAuroraSpin{to{transform:rotate(360deg)}}
@keyframes axoDrift{0%{transform:translate3d(-90px,-160px,0) rotate(34deg);opacity:0}18%{opacity:1}82%{opacity:.65}100%{transform:translate3d(160px,120vh,0) rotate(34deg);opacity:0}}
.axo-topbar{transition:border-color .25s ease,box-shadow .25s ease,background .25s ease;box-shadow:0 16px 55px rgba(0,0,0,.28),inset 0 1px rgba(255,255,255,.028)}
.top-nav button{position:relative;overflow:hidden;transition:color .2s ease,background .2s ease,transform .2s cubic-bezier(.2,.8,.2,1)}
.top-nav button:before{content:"";position:absolute;left:18%;right:18%;bottom:4px;height:2px;border-radius:999px;background:linear-gradient(90deg,transparent,#78f0dc,#a58aff,transparent);opacity:0;transform:scaleX(.3);transition:.24s ease;box-shadow:0 0 18px rgba(112,238,218,.55)}
.top-nav button.active:before{opacity:1;transform:scaleX(1)}.top-nav button:hover{transform:translateY(-1px)}
.status-chip .status-dot{box-shadow:0 0 0 0 rgba(98,241,178,.42);animation:axoPulseDot 2.1s ease-out infinite}
@keyframes axoPulseDot{0%{box-shadow:0 0 0 0 rgba(98,241,178,.38)}70%{box-shadow:0 0 0 7px rgba(98,241,178,0)}100%{box-shadow:0 0 0 0 rgba(98,241,178,0)}}
.hero-reef{transform:translateZ(0);box-shadow:0 28px 95px rgba(0,0,0,.27),inset 0 1px rgba(255,255,255,.025)}
.hero-reef:before{content:"";position:absolute;inset:0;pointer-events:none;background:linear-gradient(115deg,transparent 8%,rgba(117,242,221,.035) 38%,transparent 52%,rgba(162,137,255,.03) 77%,transparent 91%);background-size:240% 100%;animation:heroSweep 10s ease-in-out infinite}
@keyframes heroSweep{0%,100%{background-position:220% 0}50%{background-position:-120% 0}}
.portal-ring.ring-outer{animation:portalSpin 28s linear infinite}.portal-ring.ring-mid{animation:portalSpinReverse 18s linear infinite}.portal-ring.ring-inner{animation:portalBreathe 4.2s ease-in-out infinite}
.axo-creature{animation:axoFloat 4.5s ease-in-out infinite}.axo-creature .gill{animation:gillPulse 2.7s ease-in-out infinite}.axo-creature .gill:nth-child(even){animation-delay:-1.25s}
@keyframes portalSpin{to{transform:rotate(360deg)}}@keyframes portalSpinReverse{to{transform:rotate(-360deg)}}@keyframes portalBreathe{0%,100%{transform:scale(.98);opacity:.62}50%{transform:scale(1.035);opacity:1}}@keyframes axoFloat{0%,100%{transform:translateY(0) rotate(-.25deg)}50%{transform:translateY(-8px) rotate(.35deg)}}@keyframes gillPulse{0%,100%{filter:saturate(1);opacity:.82}50%{filter:saturate(1.35) brightness(1.12);opacity:1}}
.portal-tag{animation:tagHover 3.6s ease-in-out infinite}.tag-safe{animation-delay:-1.1s}.tag-core{animation-delay:-2.3s}@keyframes tagHover{0%,100%{transform:translateY(0)}50%{transform:translateY(-4px)}}
.launch-primary{position:relative;overflow:hidden;transition:transform .22s cubic-bezier(.2,.8,.2,1),box-shadow .22s ease,filter .22s ease}.launch-primary:after{content:"";position:absolute;top:-80%;bottom:-80%;width:38px;left:-80px;background:rgba(255,255,255,.34);transform:rotate(18deg);filter:blur(4px);animation:launchGlint 4.8s ease-in-out infinite}.launch-primary:hover:not(:disabled){transform:translateY(-2px) scale(1.008);filter:saturate(1.08);box-shadow:0 20px 48px rgba(105,234,216,.16)}
@keyframes launchGlint{0%,68%{left:-90px;opacity:0}73%{opacity:.8}88%{left:115%;opacity:.15}100%{left:115%;opacity:0}}
.tool-grid button,.instance-card,.project-card-new,.account-card-new,.setting-tile,.pulse-card,.quick-tools{transition:transform .22s cubic-bezier(.2,.8,.2,1),border-color .22s ease,box-shadow .22s ease,background .22s ease}.tool-grid button:hover,.project-card-new:hover,.account-card-new:hover{transform:translateY(-4px);box-shadow:0 20px 50px rgba(0,0,0,.24);border-color:rgba(126,244,225,.17)}.instance-card:hover{transform:translateY(-3px);box-shadow:0 24px 70px rgba(0,0,0,.25),inset 0 1px rgba(255,255,255,.03)}.project-card-new .project-icon-new img{transition:transform .35s cubic-bezier(.2,.8,.2,1),filter .35s ease}.project-card-new:hover .project-icon-new img{transform:scale(1.09) rotate(-2deg);filter:saturate(1.14)}
.axo-workspace>section{animation:v12PageIn .42s cubic-bezier(.16,1,.3,1)}@keyframes v12PageIn{0%{opacity:0;transform:translateY(13px) scale(.992);filter:blur(7px)}60%{filter:blur(0)}100%{opacity:1;transform:none;filter:none}}
.boot-overlay{overflow:hidden;background:radial-gradient(circle at 50% 48%,rgba(45,160,153,.13),transparent 25%),radial-gradient(circle at 50% 50%,#071217 0,#03080c 62%,#020507 100%)}.boot-atmosphere{position:absolute;inset:0;pointer-events:none}.boot-atmosphere i{position:absolute;width:3px;height:3px;border-radius:50%;background:#a9fff2;box-shadow:0 0 18px rgba(113,241,222,.8);animation:bootParticle 6s linear infinite}.boot-atmosphere i:nth-child(1){left:12%;top:78%;animation-delay:-1s}.boot-atmosphere i:nth-child(2){left:22%;top:18%;animation-delay:-3.5s}.boot-atmosphere i:nth-child(3){left:32%;top:65%;animation-delay:-5s}.boot-atmosphere i:nth-child(4){left:42%;top:24%;animation-delay:-2.2s}.boot-atmosphere i:nth-child(5){left:53%;top:82%;animation-delay:-4.1s}.boot-atmosphere i:nth-child(6){left:62%;top:14%;animation-delay:-.7s}.boot-atmosphere i:nth-child(7){left:72%;top:69%;animation-delay:-3s}.boot-atmosphere i:nth-child(8){left:84%;top:29%;animation-delay:-5.3s}.boot-atmosphere i:nth-child(9){left:92%;top:74%;animation-delay:-1.8s}.boot-atmosphere i:nth-child(10){left:6%;top:35%;animation-delay:-4.8s}.boot-atmosphere i:nth-child(11){left:47%;top:11%;animation-delay:-2.8s}.boot-atmosphere i:nth-child(12){left:76%;top:89%;animation-delay:-.3s}@keyframes bootParticle{0%{transform:translateY(30px) scale(.55);opacity:0}20%{opacity:.8}70%{opacity:.45}100%{transform:translateY(-110px) scale(1.3);opacity:0}}
.boot-radar{position:absolute;left:50%;top:50%;width:520px;height:520px;transform:translate(-50%,-50%);border:1px solid rgba(122,239,220,.06);border-radius:50%;pointer-events:none}.boot-radar:before,.boot-radar:after{content:"";position:absolute;inset:15%;border:1px solid rgba(141,132,255,.06);border-radius:50%}.boot-radar:after{inset:31%;border-color:rgba(113,240,219,.08)}.boot-radar span{position:absolute;left:50%;top:50%;width:48%;height:1px;transform-origin:0 50%;background:linear-gradient(90deg,rgba(116,241,222,.42),transparent);animation:radarSweep 3.8s linear infinite}.boot-radar b,.boot-radar i{position:absolute;border-radius:50%;border:1px dashed rgba(125,239,224,.05);inset:7%;animation:portalSpin 22s linear infinite}.boot-radar i{inset:22%;animation-duration:14s;animation-direction:reverse}@keyframes radarSweep{to{transform:rotate(360deg)}}.boot-core{position:relative;z-index:2}.boot-axo{animation:bootLogoFloat 3s ease-in-out infinite}.boot-line{box-shadow:0 0 24px rgba(107,239,219,.08)}.boot-line span{box-shadow:0 0 22px rgba(107,239,219,.5)}@keyframes bootLogoFloat{0%,100%{transform:translateY(0)}50%{transform:translateY(-7px)}}
.health-tile{grid-column:span 2}.health-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px}.health-head>div{display:grid;gap:4px}.health-head span{font-size:8px;letter-spacing:.15em;color:#70bfb3;font-weight:900}.health-head b{font-size:16px}.health-head button{height:35px;border-radius:10px;padding:0 11px;border:1px solid rgba(126,243,225,.11);background:rgba(117,242,221,.05);color:#bdfaf1;display:flex;align-items:center;gap:6px;cursor:pointer}.health-head button:disabled{opacity:.55}.health-list{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:8px;margin:7px 0 11px}.health-row{min-width:0;border:1px solid rgba(255,255,255,.05);border-radius:13px;padding:10px;background:rgba(255,255,255,.018);display:flex;align-items:flex-start;gap:8px}.health-row>i{width:8px;height:8px;border-radius:50%;margin-top:4px;flex:none;background:#75868a;box-shadow:0 0 14px currentColor}.health-row.ok>i{background:#65e9ae;color:#65e9ae}.health-row.warn>i{background:#ffd36f;color:#ffd36f}.health-row.error>i{background:#ff7994;color:#ff7994}.health-row.checking>i{background:#86b8ff;color:#86b8ff;animation:axoPulseDot 1.8s infinite}.health-row span{min-width:0;display:grid;gap:3px}.health-row b{font-size:9px;color:#dffff8}.health-row small{font-size:8px;color:#76928f;line-height:1.35;overflow-wrap:anywhere}
.microsoft-dialog{animation:modalPop .34s cubic-bezier(.16,1,.3,1)}.microsoft-orb{animation:secureOrb 3s ease-in-out infinite}@keyframes modalPop{from{opacity:0;transform:translateY(12px) scale(.96);filter:blur(6px)}to{opacity:1;transform:none;filter:none}}@keyframes secureOrb{0%,100%{box-shadow:0 0 45px rgba(113,237,220,.08)}50%{box-shadow:0 0 78px rgba(113,237,220,.18);transform:translateY(-2px)}}.console-window,.crash-window,.launch-dialog{animation:modalPop .3s cubic-bezier(.16,1,.3,1)}
@media(max-width:1050px){.health-tile{grid-column:1/-1}.health-list{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:620px){.health-list{grid-template-columns:1fr}.boot-radar{width:380px;height:380px}}
@media(prefers-reduced-motion:reduce){.axo-app:before,.motion-field span,.portal-ring,.axo-creature,.axo-creature .gill,.portal-tag,.launch-primary:after,.boot-atmosphere i,.boot-radar span,.boot-radar b,.boot-radar i,.boot-axo,.status-chip .status-dot,.hero-reef:before,.microsoft-orb{animation:none!important}.axo-workspace>section{animation:none!important;filter:none!important}}
'''
p.write_text(css, encoding='utf-8')

p = root / 'README.md'
if p.exists():
    r = p.read_text(encoding='utf-8')
    r = r.replace('0.11.2','0.12.0').replace('0.11.1','0.12.0')
    r += '''\n\n## v0.12 Motion Core\n- Premium motion layer with animated nebula, portal, cards, navigation, launch shimmer and accessibility-friendly reduced-motion fallback.\n- System Health panel checks Axolot Cloud, game monitor, updater, Modrinth and CurseForge.\n- Microsoft sign-in diagnostics surface OAuth/Xbox/XSTS/Minecraft Services errors instead of leaving the Device Code dialog stuck.\n- Existing AutoBoost, multi-account, live console, crash center, cloud-managed CurseForge and auto-updater remain included.\n'''
    p.write_text(r, encoding='utf-8')

print('Axolot v0.12 Motion Core patch applied.')
