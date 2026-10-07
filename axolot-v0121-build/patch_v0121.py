from pathlib import Path
import sys

root = Path(sys.argv[1])

def replace(path, old, new, count=-1):
    p = root / path
    t = p.read_text(encoding="utf-8")
    if old not in t:
        raise SystemExit(f"Needle not found in {path}: {old[:90]!r}")
    t = t.replace(old, new, count)
    p.write_text(t, encoding="utf-8")

replace("package.json", '"version": "0.12.0"', '"version": "0.12.1"', 1)
replace("src-tauri/Cargo.toml", 'version = "0.12.0"', 'version = "0.12.1"', 1)
replace("src-tauri/tauri.conf.json", '"version": "0.12.0"', '"version": "0.12.1"', 1)
replace("src-tauri/tauri.conf.json", "Release Candidate 0.12.0", "Release Candidate 0.12.1", 1)

main = root / "src/main.tsx"
t = main.read_text(encoding="utf-8")

needle = "const DEFAULT_UPDATE_MANIFEST = 'https://github.com/DiskiiVn/Craftopia-bot/releases/latest/download/axolot-update.json';\n"
insert = needle + """const MINECRAFT_APP_APPROVAL_URL = 'https://forms.cloud.microsoft/Pages/ResponsePage.aspx?id=v4j5cvGGr0GRqy180BHbR-ajEQ1td1ROpz00KtS8Gd5UNVpPTkVLNFVROVQxNkdRMEtXVjNQQjdXVC4u';
const MINECRAFT_APP_INFO_URL = 'https://help.minecraft.net/hc/en-us/articles/16254801392141';

const isMinecraftAppRegistrationError = (message: string) => /invalid app registration|allowlist|approved\\/allowlisted/i.test(message);
"""
if needle not in t:
    raise SystemExit("DEFAULT_UPDATE_MANIFEST needle missing")
t = t.replace(needle, insert, 1)

needle = "  const activeAccount = accounts.find((account) => account.id === activeAccountId) ?? accounts[0];\n"
if needle not in t:
    raise SystemExit("activeAccount needle missing")
t = t.replace(needle, needle + "  const microsoftNeedsApproval = isMinecraftAppRegistrationError(microsoftError);\n", 1)

needle = "  const connectMicrosoftAccount = async () => {\n"
handlers = """  const openMinecraftApproval = async () => {
    try {
      await navigator.clipboard?.writeText(microsoftClientId.trim());
    } catch {
      // Clipboard permissions are optional; the approval page still opens.
    }
    setStatus('Axolot Client ID copied. Submit it to Minecraft AppID approval, then retry after approval.');
    void invoke('open_external_url', { url: MINECRAFT_APP_APPROVAL_URL }).catch(() => {
      void invoke('open_external_url', { url: MINECRAFT_APP_INFO_URL }).catch(() => undefined);
    });
  };

  const continueOfflineAfterMicrosoftBlock = () => {
    const offline = accounts.find((account) => account.kind === 'offline');
    if (offline) {
      setActiveAccountId(offline.id);
      setLauncherName(offline.username);
    }
    setMicrosoftDevice(null);
    setMicrosoftError('');
    setMicrosoftStage('');
    setPage('play');
    setStatus('Minecraft Premium login is waiting for AppID approval. Offline mode remains available for compatible servers.');
  };

""" + needle
if needle not in t:
    raise SystemExit("connectMicrosoftAccount needle missing")
t = t.replace(needle, handlers, 1)

t = t.replace("AXOLOT NEBULA 0.12 // MOTION CORE", "AXOLOT NEBULA 0.12.1 // MOTION CORE")
t = t.replace("AXOLOT NEBULA 0.12</span>", "AXOLOT NEBULA 0.12.1</span>")

old = """            <h2>{microsoftError ? 'Microsoft signed in, but Minecraft could not finish' : 'Link your Minecraft account'}</h2>
            <p>{microsoftError ? 'Axolot reached an error after or during Microsoft authorization. The exact backend response is shown below.' : 'Open Microsoft sign-in and enter this one-time code. Axolot will continue through Xbox and Minecraft Services automatically.'}</p>
            {!microsoftError && <button className="device-code" onClick={()=>navigator.clipboard?.writeText(microsoftDevice.user_code)}>{microsoftDevice.user_code}</button>}
            {microsoftError && <div className="microsoft-error-box"><b>LOGIN DIAGNOSTIC</b><span>{microsoftError}</span></div>}
            <div className="microsoft-modal-actions">
              {microsoftError ? (
                <>
                  <button className="accent-action" onClick={()=>{setMicrosoftDevice(null);setMicrosoftError('');setMicrosoftStage('');void connectMicrosoftAccount();}}><RefreshCw size={15}/> Try again</button>
                  <button onClick={()=>{setMicrosoftDevice(null);setMicrosoftError('');setMicrosoftStage('');}}>Close</button>
                </>
              ) : (
                <>
                  <button className="accent-action" onClick={()=>void invoke('open_external_url',{url:microsoftDevice.verification_uri})}><LogIn size={15}/> Open Microsoft</button>
                  <button onClick={()=>navigator.clipboard?.writeText(microsoftDevice.user_code)}>Copy code</button>
                </>
              )}
            </div>
"""
new = """            <h2>{microsoftError ? (microsoftNeedsApproval ? 'Minecraft AppID approval required' : 'Microsoft signed in, but Minecraft could not finish') : 'Link your Minecraft account'}</h2>
            <p>{microsoftError ? (microsoftNeedsApproval ? 'Microsoft, Xbox Live and XSTS are already working. Minecraft Services is blocking this new Axolot Client ID until Mojang/Minecraft approves it.' : 'Axolot reached an error after or during Microsoft authorization. The exact backend response is shown below.') : 'Open Microsoft sign-in and enter this one-time code. Axolot will continue through Xbox and Minecraft Services automatically.'}</p>
            {!microsoftError && <button className="device-code" onClick={()=>navigator.clipboard?.writeText(microsoftDevice.user_code)}>{microsoftDevice.user_code}</button>}
            {microsoftError && <div className={"microsoft-error-box " + (microsoftNeedsApproval ? "approval" : "")}><b>{microsoftNeedsApproval ? 'MINECRAFT SERVICES GATE' : 'LOGIN DIAGNOSTIC'}</b><span>{microsoftError}</span>{microsoftNeedsApproval && <small>Axolot cannot bypass this server-side allowlist. Submit the Axolot Client ID once, then Premium login will start working without another launcher update.</small>}</div>}
            {microsoftNeedsApproval ? (
              <div className="minecraft-approval-actions">
                <button className="accent-action wide" onClick={openMinecraftApproval}><ShieldCheck size={15}/> Apply for Minecraft access</button>
                <button onClick={()=>navigator.clipboard?.writeText(microsoftClientId.trim())}>Copy Client ID</button>
                <button onClick={continueOfflineAfterMicrosoftBlock}><Play size={15}/> Continue Offline</button>
                <button onClick={()=>{setMicrosoftDevice(null);setMicrosoftError('');setMicrosoftStage('');void connectMicrosoftAccount();}}><RefreshCw size={15}/> Retry after approval</button>
              </div>
            ) : (
              <div className="microsoft-modal-actions">
                {microsoftError ? (
                  <>
                    <button className="accent-action" onClick={()=>{setMicrosoftDevice(null);setMicrosoftError('');setMicrosoftStage('');void connectMicrosoftAccount();}}><RefreshCw size={15}/> Try again</button>
                    <button onClick={()=>{setMicrosoftDevice(null);setMicrosoftError('');setMicrosoftStage('');}}>Close</button>
                  </>
                ) : (
                  <>
                    <button className="accent-action" onClick={()=>void invoke('open_external_url',{url:microsoftDevice.verification_uri})}><LogIn size={15}/> Open Microsoft</button>
                    <button onClick={()=>navigator.clipboard?.writeText(microsoftDevice.user_code)}>Copy code</button>
                  </>
                )}
              </div>
            )}
"""
if old not in t:
    raise SystemExit("Microsoft modal block missing")
t = t.replace(old, new, 1)
main.write_text(t, encoding="utf-8")

lib = root / "src-tauri/src/lib.rs"
t = lib.read_text(encoding="utf-8")
old = 'return Err("Microsoft authorization succeeded, but Minecraft Services rejected this Axolot Client ID with HTTP 403: Invalid app registration. Your Entra Client ID must be approved/allowlisted by Minecraft before third-party Premium login can finish. This is not a password or Device Code problem.".into());'
new = 'return Err("MC_APP_NOT_ALLOWLISTED: Microsoft authorization, Xbox Live and XSTS succeeded, but Minecraft Services returned HTTP 403 Invalid app registration. This Axolot Application (Client) ID must be approved through the Minecraft AppID registration process before Premium login can finish. No launcher-side OAuth setting can bypass this server-side allowlist.".into());'
if old not in t:
    raise SystemExit("Rust Minecraft 403 diagnostic missing")
t = t.replace(old, new, 1)
lib.write_text(t, encoding="utf-8")

css = root / "src/styles.css"
s = css.read_text(encoding="utf-8")
s += """
/* v0.12.1 — Minecraft AppID approval UX */
.microsoft-error-box.approval{border-color:rgba(255,197,104,.24);background:linear-gradient(145deg,rgba(255,170,72,.08),rgba(255,92,132,.055));box-shadow:inset 0 1px rgba(255,255,255,.025)}
.microsoft-error-box.approval b{color:#ffd18b}.microsoft-error-box.approval small{font-size:9px;line-height:1.5;color:#d8c7a9}
.minecraft-approval-actions{display:grid;grid-template-columns:1fr 1fr;gap:9px;margin-top:4px}.minecraft-approval-actions button{min-height:42px;border-radius:12px;border:1px solid rgba(255,255,255,.07);background:rgba(255,255,255,.035);color:#bfd0d4;display:flex;align-items:center;justify-content:center;gap:7px;cursor:pointer;transition:transform .18s ease,border-color .18s ease,background .18s ease}.minecraft-approval-actions button:hover{transform:translateY(-1px);border-color:rgba(119,241,220,.18);background:rgba(117,242,221,.06)}.minecraft-approval-actions .wide{grid-column:1/-1;background:linear-gradient(135deg,rgba(107,235,218,.18),rgba(157,124,255,.16));color:#e7fffb;border-color:rgba(117,242,221,.19)}
@media(max-width:520px){.minecraft-approval-actions{grid-template-columns:1fr}.minecraft-approval-actions .wide{grid-column:auto}}
"""
css.write_text(s, encoding="utf-8")

readme = root / "README.md"
r = readme.read_text(encoding="utf-8")
r += """

## v0.12.1 Minecraft AppID approval handling
- Detects Minecraft Services HTTP 403 Invalid app registration as a server-side AppID allowlist issue.
- Provides an in-launcher button to open the official Minecraft AppID approval form and copies the configured Axolot Client ID.
- Adds a safe Offline fallback for compatible/private servers while Premium access is awaiting approval.
- Existing v0.12.0 clients receive v0.12.1 through the built-in verified updater; users do not need to manually download a new installer.
- The launcher does not reuse another launcher's Client ID and does not attempt to bypass Minecraft Services authorization.
"""
readme.write_text(r, encoding="utf-8")
print("v0.12.1 patch applied")
