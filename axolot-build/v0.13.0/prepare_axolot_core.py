from __future__ import annotations
from pathlib import Path
import json
import re
import shutil
import sys

root = Path(sys.argv[1] if len(sys.argv) > 1 else "axolot-core").resolve()
if not (root / "build.gradle").exists():
    raise SystemExit(f"Fast Client source not found: {root}")

assets = root / "src/main/resources/assets"
if (assets / "fast").exists() and not (assets / "axolot").exists():
    (assets / "fast").rename(assets / "axolot")
for old, new in [("fast.mixins.json", "axolot.mixins.json"), ("fast.accesswidener", "axolot.accesswidener")]:
    src = root / "src/main/resources" / old
    dst = root / "src/main/resources" / new
    if src.exists() and not dst.exists():
        src.rename(dst)

text_files = list(root.rglob("*.java")) + list(root.rglob("*.gradle")) + list(root.rglob("*.properties")) + list(root.rglob("*.json")) + list(root.rglob("*.md"))
for path in text_files:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        continue
    text = text.replace("/assets/fast/", "/assets/axolot/").replace("assets/fast/", "assets/axolot/")
    text = text.replace("Fast Client", "Axolot Client").replace("FastClient", "AxolotCore")
    text = text.replace("Performance & Utility Client", "Performance · HUD · Utility · Profiles")
    path.write_text(text, encoding="utf-8")

p = root / "build.gradle"
s = p.read_text(encoding="utf-8").replace("src/main/resources/fast.accesswidener", "src/main/resources/axolot.accesswidener")
p.write_text(s, encoding="utf-8")

p = root / "gradle.properties"
s = p.read_text(encoding="utf-8")
s = re.sub(r"mod_version=.*", "mod_version=0.13.0", s)
s = re.sub(r"maven_group=.*", "maven_group=gg.axolot", s)
s = re.sub(r"archives_base_name=.*", "archives_base_name=axolot-core", s)
p.write_text(s, encoding="utf-8")

p = root / "src/main/resources/fabric.mod.json"
d = json.loads(p.read_text(encoding="utf-8"))
d.update({
    "id": "axolotcore",
    "name": "Axolot Core",
    "description": "Axolot Client in-game core for performance, HUD, profiles, music and utility modules.",
    "authors": ["Axolot Client Studio", "EldoDebug (Fast Client source)"],
    "contact": {"homepage": "https://github.com/DiskiiVn/Craftopia-bot"},
    "environment": "client",
    "mixins": ["axolot.mixins.json"],
    "accessWidener": "axolot.accesswidener",
})
p.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")

p = root / "src/main/java/com/fastclient/Fast.java"
s = p.read_text(encoding="utf-8")
s = s.replace('private final String name = "Fast";', 'private final String name = "Axolot";')
s = re.sub(r'private final String version = "[^"]+";', 'private final String version = "0.13.0";', s)
p.write_text(s, encoding="utf-8")


p = root / "src/main/java/com/fastclient/logger/FastLogger.java"
if p.exists():
    t = p.read_text(encoding="utf-8").replace('LogManager.getLogger("Axolot Client")', 'LogManager.getLogger("Axolot Core")')
    p.write_text(t, encoding="utf-8")

p = root / "src/main/java/com/fastclient/libraries/material3/Material3.java"
s = p.read_text(encoding="utf-8")
s = re.sub(r'private static final double FASTCLIENT_HUE = [0-9.]+;', 'private static final double FASTCLIENT_HUE = 195.0;', s)
s = re.sub(r'private static final double FASTCLIENT_CHROMA = [0-9.]+;', 'private static final double FASTCLIENT_CHROMA = 62.0;', s)
s = re.sub(r'private static final double FASTCLIENT_TONE = [0-9.]+;', 'private static final double FASTCLIENT_TONE = 72.0;', s)
s = re.sub(r'public static final int FASTCLIENT_ORANGE = 0x[0-9A-Fa-f]+;', 'public static final int FASTCLIENT_ORANGE = 0xFF67E8DD;', s)
s = re.sub(r'public static final int FASTCLIENT_YELLOW = 0x[0-9A-Fa-f]+;', 'public static final int FASTCLIENT_YELLOW = 0xFFA882FF;', s)
s = re.sub(r'public static final int FASTCLIENT_TEXT_ORANGE = 0x[0-9A-Fa-f]+;', 'public static final int FASTCLIENT_TEXT_ORANGE = 0xFF72D8FF;', s)
s = re.sub(r'public static final int FASTCLIENT_HOVER = 0x[0-9A-Fa-f]+;', 'public static final int FASTCLIENT_HOVER = 0xFF10222D;', s)
p.write_text(s, encoding="utf-8")

p = root / "src/main/java/com/fastclient/gui/modmenu/GuiModMenu.java"
s = p.read_text(encoding="utf-8")
s = s.replace("new NavigationRail(this, getX(), getY(), 90, getHeight())", "new NavigationRail(this, getX(), getY(), 104, getHeight())")
s = s.replace("return Math.max(600, Math.min(1200, targetWidth));", "return Math.max(720, Math.min(1320, targetWidth));")
s = s.replace("return Math.max(400, Math.min(700, targetHeight));", "return Math.max(460, Math.min(760, targetHeight));")
p.write_text(s, encoding="utf-8")

home = '''package com.fastclient.gui.modmenu.pages;\n\nimport com.fastclient.Fast;\nimport com.fastclient.gui.api.Gui;\nimport com.fastclient.gui.api.page.Page;\nimport com.fastclient.gui.api.page.impl.RightLeftTransition;\nimport com.fastclient.management.color.api.ColorPalette;\nimport com.fastclient.skia.Skia;\nimport com.fastclient.skia.font.Fonts;\nimport com.fastclient.skia.font.Icon;\nimport com.fastclient.utils.ColorUtils;\n\npublic class HomePage extends Page {\n    public HomePage(Gui parent) { super(parent, "text.home", Icon.HOME, new RightLeftTransition(true)); }\n    @Override public void draw(double mouseX, double mouseY) {\n        super.draw(mouseX, mouseY);\n        ColorPalette palette = Fast.getInstance().getColorManager().getPalette();\n        float pad = 34, top = y + 30, left = x + pad, right = x + width - pad;\n        Skia.drawText("AXOLOT CORE", left, top + 6, palette.getPrimary(), Fonts.getMedium(11));\n        Skia.drawText("Your Minecraft, tuned.", left, top + 43, palette.getOnSurface(), Fonts.getMedium(34));\n        Skia.drawText("Performance, HUD, profiles and utility modules in one in-game command layer.", left, top + 70, palette.getOnSurfaceVariant(), Fonts.getRegular(14));\n        float heroY = top + 98, heroH = Math.max(165, height - 235);\n        Skia.drawRoundedRect(left, heroY, right - left, heroH, 26, palette.getSurface());\n        Skia.drawOutline(left, heroY, right - left, heroH, 26, 1, ColorUtils.applyAlpha(palette.getPrimary(), .18F));\n        float logoSize = Math.min(116, heroH - 44);\n        Skia.drawImage("home_logo.png", left + 28, heroY + (heroH - logoSize) / 2, logoSize, logoSize);\n        float copyX = left + 28 + logoSize + 28;\n        Skia.drawText("Axolot Client", copyX, heroY + 45, palette.getOnSurface(), Fonts.getMedium(30));\n        Skia.drawText("NEBULA 0.13", copyX, heroY + 71, palette.getPrimary(), Fonts.getMedium(12));\n        Skia.drawText("Left click modules to toggle  •  Right click for settings", copyX, heroY + 103, palette.getOnSurfaceVariant(), Fonts.getRegular(13));\n        int modules = Fast.getInstance().getModManager().getMods().size();\n        int profiles = Fast.getInstance().getProfileManager().getProfiles().size();\n        float statY = heroY + heroH - 62, statW = 142;\n        drawStat(copyX, statY, statW, "MODULES", String.valueOf(modules), palette);\n        drawStat(copyX + statW + 12, statY, statW, "PROFILES", String.valueOf(profiles), palette);\n        drawStat(copyX + (statW + 12) * 2, statY, statW, "RENDER", "SKIA", palette);\n        Skia.drawCenteredText("Tip: use the edit button on the rail to arrange your HUD", x + width / 2, y + height - 28, palette.getOnSurfaceVariant(), Fonts.getRegular(11));\n    }\n    private void drawStat(float sx, float sy, float sw, String label, String value, ColorPalette palette) {\n        Skia.drawRoundedRect(sx, sy, sw, 42, 14, palette.getSurfaceContainerHigh());\n        Skia.drawText(label, sx + 12, sy + 13, palette.getOnSurfaceVariant(), Fonts.getRegular(9));\n        Skia.drawText(value, sx + 12, sy + 31, palette.getOnSurface(), Fonts.getMedium(15));\n    }\n}\n'''
(root / "src/main/java/com/fastclient/gui/modmenu/pages/HomePage.java").write_text(home, encoding="utf-8")

p = root / "src/main/java/com/fastclient/gui/modmenu/component/NavigationRail.java"
s = p.read_text(encoding="utf-8")
s = s.replace("Skia.drawRoundedRectVarying(x, y, width, height, 35, 0, 0, 35, palette.getSurface());\n\n\t\tSkia.drawRect(x + width - 1, y, 1, height, Color.WHITE);", "Skia.drawRoundedRectVarying(x, y, width, height, 30, 0, 0, 30, palette.getSurface());\n\n\t\tSkia.drawRect(x + width - 1, y + 18, 1, height - 36, ColorUtils.applyAlpha(palette.getOnSurfaceVariant(), 0.18F));")
s = s.replace("float navStartY = 120;", "float navStartY = 126;").replace("float navItemHeight = 68;", "float navItemHeight = 72;")
s = s.replace("float offsetY = 120;", "float offsetY = 126;").replace("offsetY += 68;", "offsetY += 72;")
old_block = """\t\t\tjava.awt.Color c0 = java.awt.Color.WHITE;\n\t\t\tjava.awt.Color c1 = java.awt.Color.WHITE;\n\n\t\t\tAnimation animation = n.animation;\n\t\t\tfloat selWidth = 56;\n\t\t\tfloat selHeight = 32;\n\t\t\tfloat adjustedY = y + offsetY + scrollHelper.getValue();\n\t\t\tboolean isActive = currentNavigation.equals(n);\n\n\t\t\tSkia.drawText(icon, x + (width / 2) - (iconWidth / 2), y + (offsetY + (selHeight / 2)) - (iconHeight / 2),\n\t\t\t\t\tc0, font);\n\t\t\tSkia.drawCenteredText(I18n.get(title), x + (width / 2), y + offsetY + selHeight + 5, c1,\n\t\t\t\t\tFonts.getMedium(12));\n\n\t\t\tif (isActive) {\n\t\t\t\tfloat barWidth = 32;\n\t\t\t\tfloat barHeight = 3;\n\t\t\t\tfloat barX = x + (width / 2) - (barWidth / 2);\n\t\t\t\tfloat barY = y + offsetY + selHeight + 18;\n\t\t\t\tSkia.drawRoundedRect(barX, barY, barWidth, barHeight, 1.5F, palette.getPrimary());\n\t\t\t}\n"""
new_block = """\t\t\tboolean isActive = currentNavigation.equals(n);\n\t\t\tjava.awt.Color c0 = isActive ? palette.getPrimary() : palette.getOnSurfaceVariant();\n\t\t\tjava.awt.Color c1 = isActive ? palette.getOnSurface() : palette.getOnSurfaceVariant();\n\n\t\t\tAnimation animation = n.animation;\n\t\t\tfloat selWidth = 62;\n\t\t\tfloat selHeight = 36;\n\t\t\tfloat adjustedY = y + offsetY + scrollHelper.getValue();\n\n\t\t\tif (isActive) {\n\t\t\t\tfloat pillX = x + (width / 2) - (selWidth / 2);\n\t\t\t\tSkia.drawRoundedRect(pillX, y + offsetY - 2, selWidth, selHeight + 6, 16,\n\t\t\t\t\t\tColorUtils.applyAlpha(palette.getPrimaryContainer(), 0.72F));\n\t\t\t}\n\n\t\t\tSkia.drawText(icon, x + (width / 2) - (iconWidth / 2), y + (offsetY + (selHeight / 2)) - (iconHeight / 2),\n\t\t\t\t\tc0, font);\n\t\t\tSkia.drawCenteredText(I18n.get(title), x + (width / 2), y + offsetY + selHeight + 7, c1,\n\t\t\t\t\tFonts.getMedium(11));\n"""
if old_block in s:
    s = s.replace(old_block, new_block)
p.write_text(s, encoding="utf-8")

(root / "README.md").write_text("""# Axolot Core 0.13.0\n\nAxolot Core is the in-game component bundled with Axolot Client. It is derived from the user-provided Fast Client source and reworked for the Axolot visual system.\n\nTarget: Fabric / Minecraft 1.21.4 / Java 21.\n\nThe original uploaded source did not include a LICENSE file. Preserve original attribution and verify redistribution rights before public/commercial distribution.\n""", encoding="utf-8")

print(f"Prepared Axolot Core in {root}")