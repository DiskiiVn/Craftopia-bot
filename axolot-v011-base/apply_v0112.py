from pathlib import Path

root = Path("axolot-v011-base/source")

# Version bump
for rel in ["package.json", "src-tauri/Cargo.toml", "src-tauri/tauri.conf.json"]:
    p = root / rel
    s = p.read_text(encoding="utf-8")
    s = s.replace("0.11.1", "0.11.2")
    s = s.replace("Release Candidate 0.11.1", "Release Candidate 0.11.2")
    p.write_text(s, encoding="utf-8")

# React UI: surface Microsoft/Xbox/Minecraft errors instead of leaving the device dialog stuck.
p = root / "src/main.tsx"
s = p.read_text(encoding="utf-8")

if "  RefreshCw," not in s:
    s = s.replace("  CheckCircle2,\n  Wifi,", "  CheckCircle2,\n  RefreshCw,\n  Wifi,")

if "const [microsoftError" not in s:
    s = s.replace(
        "  const [microsoftDevice, setMicrosoftDevice] = React.useState<MicrosoftDeviceCode | null>(null);",
        "  const [microsoftDevice, setMicrosoftDevice] = React.useState<MicrosoftDeviceCode | null>(null);\n"
        "  const [microsoftError, setMicrosoftError] = React.useState('');\n"
        "  const [microsoftStage, setMicrosoftStage] = React.useState('');"
    )

connect_start = s.find("  const connectMicrosoftAccount = async () => {")
connect_end = s.find("\n\n  const checkForUpdates = async () => {", connect_start)
if connect_start < 0 or connect_end < 0:
    raise RuntimeError("connectMicrosoftAccount boundaries not found")

new_connect = r"""  const connectMicrosoftAccount = async () => {
    const clientId = microsoftClientId.trim();
    if (!clientId) {
      setPage('settings');
      setStatus('Microsoft sign-in is not configured by the Axolot administrator yet.');
      return;
    }
    if (microsoftBusy) return;
    setMicrosoftBusy(true);
    setMicrosoftError('');
    setMicrosoftStage('Requesting a secure Microsoft device code…');
    setStatus('Starting Microsoft secure sign-in...');
    try {
      const device = await invoke<MicrosoftDeviceCode>('begin_microsoft_login', { clientId });
      setMicrosoftDevice(device);
      setMicrosoftStage('Waiting for Microsoft authorization, then Xbox and Minecraft verification…');
      setStatus(`Microsoft sign-in code: ${device.user_code}`);
      void invoke('open_external_url', { url: device.verification_uri }).catch(() => undefined);
      const result = await invoke<MicrosoftLoginResult>('complete_microsoft_login', {
        clientId,
        deviceCode: device.device_code,
        expiresIn: device.expires_in,
        interval: device.interval,
      });
      setMicrosoftStage('Minecraft profile verified. Finishing sign-in…');
      const next: LauncherAccount = {
        id: result.account_id, username: result.username, kind: 'microsoft', createdAt: Date.now(), uuid: result.uuid, xuid: result.xuid,
      };
      setAccounts((current) => {
        const existing = current.findIndex((item) => item.id === next.id);
        if (existing < 0) return [...current, next];
        return current.map((item) => item.id === next.id ? { ...item, ...next } : item);
      });
      setActiveAccountId(next.id);
      setMicrosoftDevice(null);
      setMicrosoftError('');
      setMicrosoftStage('');
      setStatus(`Signed in with Microsoft as ${result.username}.`);
    } catch (error) {
      const message = String(error).replace(/^Error: /, '');
      setMicrosoftError(message);
      setMicrosoftStage('Sign-in stopped. See the exact error below.');
      setStatus(message);
    } finally {
      setMicrosoftBusy(false);
    }
  };"""

s = s[:connect_start] + new_connect + s[connect_end:]

overlay_start = s.find("      {microsoftDevice && (")
overlay_end = s.find("\n\n      {consoleOpen && (", overlay_start)
if overlay_start < 0 or overlay_end < 0:
    raise RuntimeError("Microsoft overlay boundaries not found")

new_overlay = r"""      {microsoftDevice && (
        <div className="microsoft-overlay">
          <div className={`microsoft-dialog panel-glass ${microsoftError ? 'has-error' : ''}`}>
            <span className="micro-label">MICROSOFT SECURE SIGN-IN</span>
            <div className="microsoft-orb">{microsoftError ? <AlertTriangle size={30}/> : <ShieldCheck size={30}/>}</div>
            <h2>{microsoftError ? 'Microsoft signed in, but Minecraft could not finish' : 'Link your Minecraft account'}</h2>
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
            <small>{microsoftStage || (microsoftBusy ? 'Waiting for Microsoft authorization…' : microsoftDevice.message)}</small>
          </div>
        </div>
      )}"""

s = s[:overlay_start] + new_overlay + s[overlay_end:]
p.write_text(s, encoding="utf-8")

# Diagnostic styles
p = root / "src/styles.css"
s = p.read_text(encoding="utf-8")
if ".microsoft-error-box{" not in s:
    s += """
.microsoft-dialog.has-error{border-color:rgba(255,112,139,.28);box-shadow:0 40px 120px rgba(0,0,0,.52),0 0 70px rgba(255,91,128,.08)}
.microsoft-dialog.has-error .microsoft-orb{color:#ffb2c0;background:linear-gradient(145deg,rgba(255,102,132,.14),rgba(151,119,255,.12));border-color:rgba(255,126,151,.18)}
.microsoft-error-box{margin:20px 0 14px;padding:14px 15px;border-radius:14px;text-align:left;border:1px solid rgba(255,116,143,.20);background:rgba(255,76,111,.07);display:grid;gap:7px;max-height:180px;overflow:auto}
.microsoft-error-box b{font-size:9px;letter-spacing:.15em;color:#ff9eb1}.microsoft-error-box span{font-size:10px;line-height:1.55;color:#ffd5dd;word-break:break-word}
"""
p.write_text(s, encoding="utf-8")

# Rust: preserve the Minecraft Services response body and detect app allowlist rejection.
p = root / "src-tauri/src/lib.rs"
s = p.read_text(encoding="utf-8")

old_oauth = '            _ => return Err(value.get("error_description").and_then(Value::as_str).unwrap_or("Microsoft token exchange failed.").to_string()),'
if old_oauth in s:
    s = s.replace(
        old_oauth,
        '''            other => {
                let description = value.get("error_description").and_then(Value::as_str).unwrap_or("Microsoft token exchange failed.");
                return Err(format!("Microsoft OAuth error {}: {}", if other.is_empty() { "unknown" } else { other }, description));
            },'''
    )

old_mc = '''    let minecraft_auth: Value = client.post("https://api.minecraftservices.com/authentication/login_with_xbox")
        .json(&serde_json::json!({"identityToken": format!("XBL3.0 x={uhs};{xsts_token}")}))
        .send().await.map_err(|e| e.to_string())?.error_for_status().map_err(|e| format!("Minecraft Services login failed: {e}"))?
        .json().await.map_err(|e| e.to_string())?;
    let mc_token = minecraft_auth.get("access_token").and_then(Value::as_str).ok_or("Minecraft Services response missing access token")?.to_string();'''

new_mc = '''    let minecraft_response = client.post("https://api.minecraftservices.com/authentication/login_with_xbox")
        .json(&serde_json::json!({"identityToken": format!("XBL3.0 x={uhs};{xsts_token}")}))
        .send().await.map_err(|e| format!("Minecraft Services network error: {e}"))?;
    let minecraft_status = minecraft_response.status();
    let minecraft_text = minecraft_response.text().await.map_err(|e| format!("Could not read Minecraft Services response: {e}"))?;
    if !minecraft_status.is_success() {
        let parsed: Value = serde_json::from_str(&minecraft_text).unwrap_or(Value::Null);
        let detail = parsed.get("errorMessage").and_then(Value::as_str)
            .or_else(|| parsed.get("error").and_then(Value::as_str))
            .or_else(|| parsed.get("message").and_then(Value::as_str))
            .unwrap_or(minecraft_text.as_str());
        let lower = detail.to_ascii_lowercase();
        if minecraft_status.as_u16() == 403 && (lower.contains("invalid app registration") || lower.contains("app registration")) {
            return Err("Microsoft authorization succeeded, but Minecraft Services rejected this Axolot Client ID with HTTP 403: Invalid app registration. Your Entra Client ID must be approved/allowlisted by Minecraft before third-party Premium login can finish. This is not a password or Device Code problem.".into());
        }
        return Err(format!("Minecraft Services login failed (HTTP {}): {}", minecraft_status.as_u16(), detail.chars().take(500).collect::<String>()));
    }
    let minecraft_auth: Value = serde_json::from_str(&minecraft_text).map_err(|e| format!("Minecraft Services returned invalid JSON: {e}"))?;
    let mc_token = minecraft_auth.get("access_token").and_then(Value::as_str).ok_or("Minecraft Services response missing access token")?.to_string();'''

if old_mc not in s:
    raise RuntimeError("Minecraft Services login block not found")
s = s.replace(old_mc, new_mc)
p.write_text(s, encoding="utf-8")

print("Axolot v0.11.2 Microsoft diagnostic fix applied.")
