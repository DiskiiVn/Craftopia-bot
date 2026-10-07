from pathlib import Path
import json

root = Path("axolot-v092-build/source")
main = root / "src/main.tsx"
styles = root / "src/styles.css"
lib = root / "src-tauri/src/lib.rs"
pkg = root / "package.json"
conf = root / "src-tauri/tauri.conf.json"

s = main.read_text(encoding="utf-8")

s = s.replace(
    "const [catalogProjects, setCatalogProjects] = React.useState<ModrinthProject[]>(fallbackMods);",
    "const [catalogProjects, setCatalogProjects] = React.useState<ModrinthProject[]>([]);",
)
s = s.replace(
    "    setCatalogProjects(tab === 'modpacks' ? fallbackModpacks : fallbackMods);",
    "    setCatalogProjects([]);",
)

old_open = """  const openModLibrary = (profile: Profile, tab: ModLibraryTab = 'installed') => {
    setActiveProfileId(profile.id);
    setManagingModsProfileId(profile.id);
    setCreatingProfile(false);
    setPage('profiles');
    setModLibraryTab(tab);
    setCatalogQuery('');
    setCatalogProjects([]);
    void refreshLocalMods(profile.id);
  };
"""
new_open = """  const ensureProfileOptimization = async (profile: Profile) => {
    if (profile.loader !== 'Fabric' || profile.autoOptimized) {
      await refreshLocalMods(profile.id);
      return;
    }
    setCatalogMessage('Preparing this Fabric profile • installing Axolot AutoBoost mods…');
    try {
      const result = await invoke<OptimizationPackResult>('install_optimization_pack', {
        profileId: profile.id,
        gameVersion: profile.versionId,
        loader: profile.loader,
        ramGb: profile.ram,
      });
      setProfiles((current) => current.map((item) => item.id === profile.id
        ? { ...item, autoOptimized: true, optimizationPreset: result.preset }
        : item));
      setCatalogMessage(result.message);
    } catch (error) {
      setCatalogMessage(\`AutoBoost could not finish: \${String(error)}\`);
    } finally {
      await refreshLocalMods(profile.id);
    }
  };

  const openModLibrary = (profile: Profile, tab: ModLibraryTab = 'installed') => {
    setActiveProfileId(profile.id);
    setManagingModsProfileId(profile.id);
    setCreatingProfile(false);
    setPage('profiles');
    setModLibraryTab(tab);
    setCatalogQuery('');
    setCatalogProjects([]);
    void ensureProfileOptimization(profile);
  };
"""
if old_open not in s:
    raise SystemExit("openModLibrary needle not found")
s = s.replace(old_open, new_open)

start = s.index("  const searchCatalog = async (type: 'mod' | 'modpack') => {")
end = s.index("\n  const installCatalogProject = async", start)
new_search = """  const searchCatalog = async (type: 'mod' | 'modpack', sourceOverride?: CatalogSource, queryOverride?: string) => {
    if (!managedProfile) return;
    const source = sourceOverride ?? catalogSource;
    const query = queryOverride ?? catalogQuery;
    setCatalogLoading(true);
    setCatalogProjects([]);
    setCatalogMessage(\`Loading real \${source === 'modrinth' ? 'Modrinth' : 'CurseForge'} \${type === 'mod' ? 'mods' : 'modpacks'} for \${managedProfile.versionId} / \${managedProfile.loader}...\`);
    try {
      const result = source === 'modrinth'
        ? await invoke<ModrinthProject[]>('search_modrinth', {
            query,
            projectType: type,
            gameVersion: managedProfile.versionId,
            loader: managedProfile.loader,
          })
        : await invoke<ModrinthProject[]>('search_curseforge', {
            apiKey: curseForgeApiKey.trim(),
            query,
            projectType: type,
            gameVersion: managedProfile.versionId,
            loader: managedProfile.loader,
          });
      setCatalogProjects(result.map((item) => ({ ...item, source })));
      setCatalogMessage(\`\${result.length} real compatible result(s) loaded from \${source === 'modrinth' ? 'Modrinth' : 'CurseForge'}.\`);
    } catch (error) {
      setCatalogProjects([]);
      setCatalogMessage(typeof error === 'string' ? error : \`Could not reach \${source}.\`);
    } finally {
      setCatalogLoading(false);
    }
  };

  React.useEffect(() => {
    if (!managingModsProfileId || !managedProfile) return;
    if (modLibraryTab !== 'browse-mods' && modLibraryTab !== 'modpacks') return;
    const type = modLibraryTab === 'modpacks' ? 'modpack' : 'mod';
    void searchCatalog(type, catalogSource, '');
  }, [managingModsProfileId, modLibraryTab, catalogSource, managedProfile?.versionId, managedProfile?.loader]);
"""
s = s[:start] + new_search + s[end:]

s = s.replace(
    "onClick={()=>{setModLibraryTab('browse-mods');setCatalogProjects(fallbackMods);}}",
    "onClick={()=>{setModLibraryTab('browse-mods');setCatalogProjects([]);}}",
)
s = s.replace(
    "onClick={()=>{setModLibraryTab('modpacks');setCatalogProjects(fallbackModpacks);}}",
    "onClick={()=>{setModLibraryTab('modpacks');setCatalogProjects([]);}}",
)
s = s.replace(
    "onClick={()=>{setCatalogSource('modrinth');setCatalogProjects(modLibraryTab==='modpacks'?fallbackModpacks:fallbackMods);setCatalogMessage('Modrinth selected • public API ready.');}}",
    "onClick={()=>{setCatalogSource('modrinth');setCatalogProjects([]);setCatalogMessage('Loading real Modrinth catalog…');}}",
)
s = s.replace(
    "onClick={()=>{setCatalogSource('curseforge');setCatalogProjects([]);setCatalogMessage(curseForgeApiKey.trim()?'CurseForge selected • compatibility filters ready.':'CurseForge selected • add your developer API key in Settings first.');}}",
    "onClick={()=>{setCatalogSource('curseforge');setCatalogProjects([]);setCatalogMessage(curseForgeApiKey.trim()?'Loading real CurseForge catalog…':'CurseForge requires your API key in Settings.');}}",
)

grid_needle = """                <div className="project-grid-new">
                  {catalogProjects.map((project,index)=>("""
grid_repl = """                <div className="project-grid-new">
                  {catalogLoading && Array.from({length:6}).map((_,index)=><div className="project-skeleton" key={\`skeleton-\${index}\`}><span/><b/><i/></div>)}
                  {!catalogLoading && catalogProjects.length===0 && <div className="catalog-empty"><Search size={28}/><h3>No compatible results loaded</h3><p>{catalogMessage}</p></div>}
                  {catalogProjects.map((project,index)=>("""
if grid_needle not in s:
    raise SystemExit("project grid needle not found")
s = s.replace(grid_needle, grid_repl)

account_old = """<span><small>PLAYING AS</small><b>{activeAccount?.username || launcherName}</b></span>
                  <span className="account-kind">{activeAccount?.kind === 'microsoft' ? 'MICROSOFT' : 'OFFLINE'}</span>"""
account_new = """<span className="active-account-copy"><small>PLAYING AS</small><b>{activeAccount?.username || launcherName}</b></span>
                  <span className={\`account-kind \${activeAccount?.kind === 'microsoft' ? 'microsoft' : 'offline'}\`}>{activeAccount?.kind === 'microsoft' ? 'MICROSOFT' : 'OFFLINE'}</span>"""
if account_old not in s:
    raise SystemExit("account pill needle not found")
s = s.replace(account_old, account_new)

main.write_text(s, encoding="utf-8")

css = r'''
/* ==============================
   AXOLOT v0.10.1 — REAL CATALOG + ACCOUNT CHIP FIX
   ============================== */
.axo-brand:after{content:"0.10.1 RC"!important}
.hero-reef:after{content:"AXOLOT // NEBULA 0.10.1 // REAL MOD CATALOG"!important}
.active-account-pill{
  margin-top:10px;max-width:590px;width:100%;min-height:58px;padding:9px 12px;border-radius:16px;
  border:1px solid rgba(143,248,230,.12);background:linear-gradient(90deg,rgba(117,242,221,.055),rgba(159,141,255,.03));
  color:var(--text);display:grid;grid-template-columns:36px minmax(0,1fr) auto;align-items:center;gap:10px;cursor:pointer;
  box-shadow:inset 0 1px rgba(255,255,255,.025);transition:.2s ease;text-align:left;
}
.active-account-pill:hover{transform:translateY(-1px);border-color:rgba(143,248,230,.23);background:linear-gradient(90deg,rgba(117,242,221,.09),rgba(159,141,255,.055))}
.active-account-pill .user-avatar{width:36px;height:36px;border-radius:12px}
.active-account-copy{min-width:0;display:flex;flex-direction:column;align-items:flex-start;line-height:1.08}
.active-account-copy small{font-size:7px;letter-spacing:.16em;color:#6fbfb2;font-weight:800}
.active-account-copy b{display:block;max-width:100%;margin-top:5px;font-size:13px;color:#effffb;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.account-kind{height:26px;padding:0 9px;border-radius:9px;display:inline-flex;align-items:center;justify-content:center;font-size:7px;font-weight:900;letter-spacing:.12em;border:1px solid rgba(255,255,255,.06)}
.account-kind.offline{color:#a4d9cf;background:rgba(117,242,221,.045);border-color:rgba(117,242,221,.10)}
.account-kind.microsoft{color:#bbcbff;background:rgba(112,145,255,.07);border-color:rgba(112,145,255,.14)}
.project-skeleton{min-height:250px;border-radius:20px;border:1px solid rgba(255,255,255,.05);background:linear-gradient(160deg,rgba(16,34,36,.7),rgba(8,18,21,.84));padding:16px;overflow:hidden;position:relative}
.project-skeleton:after{content:"";position:absolute;inset:0;transform:translateX(-110%);background:linear-gradient(90deg,transparent,rgba(255,255,255,.045),transparent);animation:catalogShimmer 1.2s infinite}
.project-skeleton span{display:block;width:46px;height:46px;border-radius:14px;background:rgba(117,242,221,.07)}
.project-skeleton b{display:block;width:54%;height:15px;border-radius:7px;background:rgba(255,255,255,.07);margin-top:32px}
.project-skeleton i{display:block;width:88%;height:9px;border-radius:6px;background:rgba(255,255,255,.04);margin-top:14px}
@keyframes catalogShimmer{to{transform:translateX(110%)}}
.catalog-empty{grid-column:1/-1;min-height:250px;border-radius:20px;border:1px dashed rgba(143,248,230,.12);background:rgba(255,255,255,.015);display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:28px;color:#70958e}
.catalog-empty h3{margin:12px 0 6px;color:#d9fff7;font-size:15px}.catalog-empty p{max-width:540px;margin:0;font-size:10px;line-height:1.6}
@media(max-width:700px){.active-account-pill{grid-template-columns:36px minmax(0,1fr)}.account-kind{grid-column:2;justify-self:start}}
'''
with styles.open("a", encoding="utf-8") as f:
    f.write("\n" + css + "\n")

r = lib.read_text(encoding="utf-8")
r = r.replace('const USER_AGENT: &str = "AxolotClient/0.9.2 (+https://axolot.local)";', 'const USER_AGENT: &str = "AxolotClient/0.10.1 (+https://axolot.local)";')
r = r.replace(
    '.query(&[("query", query.as_str()), ("facets", facets.as_str()), ("limit", "30"), ("index", "relevance")])',
    '.query(&[("query", query.as_str()), ("facets", facets.as_str()), ("limit", "30"), ("index", if query.trim().is_empty() { "downloads" } else { "relevance" })])',
)
lib.write_text(r, encoding="utf-8")

pkg_data = json.loads(pkg.read_text(encoding="utf-8-sig"))
pkg_data["version"] = "0.10.1"
pkg.write_text(json.dumps(pkg_data, indent=2), encoding="utf-8")

conf_data = json.loads(conf.read_text(encoding="utf-8-sig"))
conf_data["version"] = "0.10.1"
conf_data["app"]["windows"][0]["title"] = "Axolot Client - Release Candidate 0.10.1"
conf.write_text(json.dumps(conf_data, indent=2), encoding="utf-8")

final_main = main.read_text(encoding="utf-8")
final_styles = styles.read_text(encoding="utf-8")
final_lib = lib.read_text(encoding="utf-8")
if "Loading real Modrinth catalog" not in final_main:
    raise SystemExit("real catalog patch missing")
if "active-account-copy" not in final_main or "active-account-pill" not in final_styles:
    raise SystemExit("account chip patch missing")
if 'AxolotClient/0.10.1' not in final_lib:
    raise SystemExit("backend version patch missing")
print("Axolot v0.10.1 patch applied")
