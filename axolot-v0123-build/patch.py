from pathlib import Path
import sys

root = Path(sys.argv[1])

def replace_in(rel, old, new):
    p = root / rel
    text = p.read_text(encoding="utf-8")
    if old not in text:
        raise SystemExit(f"Patch marker missing in {rel}: {old[:120]!r}")
    p.write_text(text.replace(old, new), encoding="utf-8")

# Version bump
for rel in ["package.json", "src-tauri/tauri.conf.json", "src-tauri/Cargo.toml", "README.md", "src/main.tsx"]:
    p = root / rel
    text = p.read_text(encoding="utf-8").replace("0.12.2", "0.12.3")
    p.write_text(text, encoding="utf-8")

# Add update prompt state.
replace_in(
    "src/main.tsx",
    """  const [updateStatus, setUpdateStatus] = React.useState<LauncherUpdateStatus | null>(null);
  const [updatingLauncher, setUpdatingLauncher] = React.useState(false);""",
    """  const [updateStatus, setUpdateStatus] = React.useState<LauncherUpdateStatus | null>(null);
  const [updatePromptOpen, setUpdatePromptOpen] = React.useState(false);
  const [updatingLauncher, setUpdatingLauncher] = React.useState(false);"""
)

# Startup check: show update UI instead of silently installing.
replace_in(
    "src/main.tsx",
    """        setUpdateStatus(update);
        if (update.available && autoInstallUpdates) {
          setBootPercent(76);
          setBootMessage(\`Updating Axolot Client to \${update.latest_version}...\`);
          setUpdatingLauncher(true);
          await invoke<string>('install_launcher_update', { downloadUrl: update.download_url, sha256: update.sha256 });
          return;
        }""",
    """        setUpdateStatus(update);
        if (update.available) {
          setBootMessage(\`Axolot \${update.latest_version} is ready to install.\`);
          const dismissed = sessionStorage.getItem('axolot.dismissedUpdateVersion');
          if (dismissed !== update.latest_version) setUpdatePromptOpen(true);
        }"""
)

# Background checks: focus + every 10 minutes.
replace_in(
    "src/main.tsx",
    """    return () => { cancelled = true; };
  }, []);

  React.useEffect(() => {
    if (!consoleOpen) return;""",
    """    return () => { cancelled = true; };
  }, []);

  React.useEffect(() => {
    if (booting || !autoInstallUpdates) return;
    let cancelled = false;
    const silentCheck = async () => {
      try {
        const next = await invoke<LauncherUpdateStatus>('check_launcher_update', { manifestUrl: updateManifestUrl.trim() || DEFAULT_UPDATE_MANIFEST });
        if (cancelled) return;
        setUpdateStatus(next);
        if (next.available) {
          const dismissed = sessionStorage.getItem('axolot.dismissedUpdateVersion');
          if (dismissed !== next.latest_version) setUpdatePromptOpen(true);
        }
      } catch {
        // Background update checks should never interrupt gameplay or navigation.
      }
    };
    const timer = window.setInterval(() => void silentCheck(), 10 * 60 * 1000);
    const onFocus = () => void silentCheck();
    window.addEventListener('focus', onFocus);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
      window.removeEventListener('focus', onFocus);
    };
  }, [booting, autoInstallUpdates, updateManifestUrl]);

  React.useEffect(() => {
    if (!consoleOpen) return;"""
)

# Manual check opens the same release prompt.
replace_in(
    "src/main.tsx",
    """      setUpdateStatus(next);
      setStatus(next.available ? \`Axolot \${next.latest_version} is available.\` : \`Axolot \${next.current_version} is up to date.\`);""",
    """      setUpdateStatus(next);
      if (next.available) {
        sessionStorage.removeItem('axolot.dismissedUpdateVersion');
        setUpdatePromptOpen(true);
      }
      setStatus(next.available ? \`Axolot \${next.latest_version} is available.\` : \`Axolot \${next.current_version} is up to date.\`);"""
)

replace_in(
    "src/main.tsx",
    """    setUpdatingLauncher(true);
    setStatus(\`Downloading Axolot \${updateStatus.latest_version}...\`);""",
    """    setUpdatingLauncher(true);
    setUpdatePromptOpen(true);
    setStatus(\`Downloading Axolot \${updateStatus.latest_version}...\`);"""
)

# Persistent topbar indicator.
replace_in(
    "src/main.tsx",
    """        <div className="top-actions">
          <button className={\`status-chip game-\${gameStatus?.state || 'stopped'}\`}""",
    """        <div className="top-actions">
          {updateStatus?.available && <button className="update-available-chip" onClick={()=>setUpdatePromptOpen(true)}><Download size={14}/><span>Update</span><b>v{updateStatus.latest_version}</b></button>}
          <button className={\`status-chip game-\${gameStatus?.state || 'stopped'}\`}"""
)

# Settings copy: automatic alert/check behavior.
replace_in(
    "src/main.tsx",
    """              <div className="setting-tile panel-glass update-setting"><span>Automatic updates</span><b>{updateStatus?.available ? \`v\${updateStatus.latest_version} available\` : 'Stable channel'}</b><label className="toggle-row"><input type="checkbox" checked={autoInstallUpdates} onChange={(e)=>setAutoInstallUpdates(e.target.checked)}/> Download and install updates automatically</label><div className="setting-actions"><button onClick={checkForUpdates}>Check now</button>{updateStatus?.available && <button className="accent-action" onClick={installUpdateNow} disabled={updatingLauncher}>{updatingLauncher ? 'Updating…' : 'Update now'}</button>}</div><small>Updates are downloaded in-app, SHA-256 verified, then installed silently. No new installer download is required from the user.</small></div>""",
    """              <div className="setting-tile panel-glass update-setting"><span>Automatic update alerts</span><b>{updateStatus?.available ? \`v\${updateStatus.latest_version} available\` : 'Stable channel'}</b><label className="toggle-row"><input type="checkbox" checked={autoInstallUpdates} onChange={(e)=>setAutoInstallUpdates(e.target.checked)}/> Check automatically and show new releases on screen</label><div className="setting-actions"><button onClick={checkForUpdates}>Check now</button>{updateStatus?.available && <button className="accent-action" onClick={()=>setUpdatePromptOpen(true)}>View update</button>}</div><small>Axolot checks at startup and while the launcher is open. Updates are SHA-256 verified and installed in-app only after you press Update now.</small></div>"""
)

# Update release modal before Microsoft overlay.
replace_in(
    "src/main.tsx",
    """      {microsoftDevice && (
        <div className="microsoft-overlay">""",
    """      {updatePromptOpen && updateStatus?.available && (
        <div className="update-overlay">
          <div className="update-dialog panel-glass">
            <div className="update-radiance" aria-hidden="true"><i/><i/><i/></div>
            <div className="update-head">
              <span className="update-glyph"><Download size={22}/></span>
              <div><span className="micro-label">AXOLOT UPDATE CHANNEL</span><h2>Axolot {updateStatus.latest_version} is ready</h2></div>
              {!updateStatus.mandatory && !updatingLauncher && <button className="update-close" onClick={()=>{sessionStorage.setItem('axolot.dismissedUpdateVersion', updateStatus.latest_version);setUpdatePromptOpen(false);}}><X size={16}/></button>}
            </div>
            <p className="update-lead">A new Axolot Client release was found automatically. You do not need to scan or download another installer from the website.</p>
            <div className="update-version-rail"><span><small>INSTALLED</small><b>v{updateStatus.current_version}</b></span><i><ChevronRight size={16}/></i><span className="next"><small>AVAILABLE</small><b>v{updateStatus.latest_version}</b></span></div>
            <div className="update-notes"><span className="micro-label">WHAT'S NEW</span><p>{updateStatus.notes || 'Performance, stability and launcher experience improvements.'}</p></div>
            <div className="update-trust"><ShieldCheck size={15}/><span>Installer is downloaded inside Axolot and verified against the release SHA-256 before installation.</span></div>
            <div className="update-actions">
              <button className="update-now" onClick={installUpdateNow} disabled={updatingLauncher}><Download size={16}/>{updatingLauncher ? 'Downloading & verifying…' : 'Update now'}</button>
              {!updateStatus.mandatory && <button disabled={updatingLauncher} onClick={()=>{sessionStorage.setItem('axolot.dismissedUpdateVersion', updateStatus.latest_version);setUpdatePromptOpen(false);}}>Later</button>}
            </div>
            <small className="update-foot">Automatic update alerts are enabled. Axolot checks again when the app regains focus and every 10 minutes while open.</small>
          </div>
        </div>
      )}

      {microsoftDevice && (
        <div className="microsoft-overlay">"""
)

css = root / "src/styles.css"
css_text = css.read_text(encoding="utf-8")
css_text += r'''

/* v0.12.3 automatic update surface */
.update-available-chip{height:38px;padding:0 12px;border-radius:13px;border:1px solid rgba(117,242,221,.22);background:linear-gradient(135deg,rgba(90,229,211,.12),rgba(146,119,255,.12));color:#d9fff8;display:flex;align-items:center;gap:7px;cursor:pointer;box-shadow:0 0 28px rgba(90,229,211,.07);animation:updateChipPulse 2.8s ease-in-out infinite}.update-available-chip span{font-size:10px;color:#8fa9ab}.update-available-chip b{font-size:10px;color:#dffef8}.update-overlay{position:fixed;inset:0;z-index:365;background:rgba(1,5,8,.76);backdrop-filter:blur(20px) saturate(125%);display:grid;place-items:center;padding:24px}.update-dialog{position:relative;overflow:hidden;width:min(560px,94vw);border-radius:28px;padding:27px;border-color:rgba(117,242,221,.20);box-shadow:0 48px 150px rgba(0,0,0,.56),0 0 80px rgba(98,225,211,.07);animation:updateDialogIn .42s cubic-bezier(.2,.85,.2,1) both}.update-radiance{position:absolute;inset:0;pointer-events:none;overflow:hidden}.update-radiance i{position:absolute;border-radius:50%;filter:blur(1px);opacity:.34}.update-radiance i:nth-child(1){width:260px;height:260px;right:-100px;top:-150px;background:radial-gradient(circle,rgba(108,238,220,.26),transparent 68%);animation:updateOrb 5.5s ease-in-out infinite}.update-radiance i:nth-child(2){width:220px;height:220px;left:-110px;bottom:-130px;background:radial-gradient(circle,rgba(161,125,255,.22),transparent 68%);animation:updateOrb 6.7s ease-in-out infinite reverse}.update-radiance i:nth-child(3){width:120px;height:120px;left:48%;top:20%;border:1px solid rgba(117,242,221,.09);animation:portalSpin 10s linear infinite}.update-head{position:relative;display:flex;align-items:center;gap:13px}.update-head h2{font-size:24px;margin:3px 0 0}.update-glyph{width:48px;height:48px;border-radius:15px;display:grid;place-items:center;color:#d9fff8;background:linear-gradient(145deg,rgba(102,232,220,.15),rgba(151,119,255,.15));border:1px solid rgba(117,242,221,.15)}.update-close{margin-left:auto;width:36px;height:36px;border-radius:11px;border:1px solid rgba(255,255,255,.07);background:rgba(255,255,255,.03);color:#8fa5a9;display:grid;place-items:center;cursor:pointer}.update-lead{position:relative;color:#93aaad;font-size:12px;line-height:1.65;margin:17px 0}.update-version-rail{position:relative;display:grid;grid-template-columns:1fr 36px 1fr;align-items:center;gap:8px;padding:13px;border-radius:17px;background:rgba(255,255,255,.025);border:1px solid rgba(255,255,255,.055)}.update-version-rail>span{display:flex;flex-direction:column;gap:3px;padding:4px 8px}.update-version-rail small{font-size:8px;letter-spacing:.14em;color:#66868a}.update-version-rail b{font-size:16px}.update-version-rail i{display:grid;place-items:center;color:#657f83}.update-version-rail .next{border-radius:12px;background:linear-gradient(135deg,rgba(101,232,217,.08),rgba(153,120,255,.08))}.update-version-rail .next b{color:#cffff7}.update-notes{position:relative;margin-top:12px;padding:15px 16px;border-radius:17px;border:1px solid rgba(117,242,221,.10);background:rgba(6,20,24,.56)}.update-notes p{font-size:11px;color:#9bb0b3;line-height:1.65;margin:7px 0 0}.update-trust{position:relative;display:flex;align-items:flex-start;gap:8px;margin:13px 2px;color:#769397;font-size:10px;line-height:1.5}.update-trust svg{color:#6ce8d6;flex:none;margin-top:1px}.update-actions{position:relative;display:grid;grid-template-columns:1.45fr .75fr;gap:9px;margin-top:16px}.update-actions button{height:44px;border-radius:13px;border:1px solid rgba(255,255,255,.07);background:rgba(255,255,255,.035);color:#afc1c4;font-weight:800;display:flex;align-items:center;justify-content:center;gap:8px;cursor:pointer}.update-actions .update-now{border:0;color:#061316;background:linear-gradient(90deg,#69eadb,#7fcff2,#a68df5);box-shadow:0 12px 34px rgba(103,232,218,.14)}.update-actions button:disabled{opacity:.58;cursor:default}.update-foot{position:relative;display:block;margin-top:12px;color:#58777b;font-size:8px;text-align:center;line-height:1.45}@keyframes updateDialogIn{from{opacity:0;transform:translateY(16px) scale(.97)}to{opacity:1;transform:none}}@keyframes updateChipPulse{50%{box-shadow:0 0 38px rgba(91,231,211,.16);border-color:rgba(117,242,221,.34)}}@keyframes updateOrb{50%{transform:translate3d(10px,-8px,0) scale(1.07)}}@media(max-width:720px){.update-available-chip span{display:none}.update-dialog{padding:21px}.update-version-rail{grid-template-columns:1fr 28px 1fr}.update-actions{grid-template-columns:1fr}.update-head h2{font-size:20px}}@media(prefers-reduced-motion:reduce){.update-available-chip,.update-dialog,.update-radiance i{animation:none!important}}
'''
css.write_text(css_text, encoding="utf-8")
