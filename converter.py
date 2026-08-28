from __future__ import annotations

import json
import re
import shutil
import stat
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml


IDENTIFIER = re.compile(r"^[a-z0-9_.-]+:[a-z0-9_./-]+$")
SAFE_MATERIAL = re.compile(r"^[a-z0-9_.-]+(?::[a-z0-9_./-]+)?$")
MAX_FILES = 20_000
MAX_UNCOMPRESSED = 300 * 1024 * 1024
MAX_SINGLE_FILE = 60 * 1024 * 1024


class ConversionError(Exception):
    pass


@dataclass(frozen=True)
class ConversionSummary:
    item_count: int
    warning_count: int


@dataclass(frozen=True)
class ItemModel:
    identifier: str
    texture_identifier: str
    source_texture: Path
    handheld: bool


@dataclass(frozen=True)
class OraxenItemConfig:
    item_id: str
    material: str
    display_name: str | None
    model_hint: str | None


def _safe_extract(source: Path, destination: Path) -> None:
    try:
        archive = zipfile.ZipFile(source)
    except zipfile.BadZipFile as exc:
        raise ConversionError("ZIP bị hỏng hoặc không hợp lệ") from exc
    with archive:
        members = archive.infolist()
        if len(members) > MAX_FILES:
            raise ConversionError("ZIP chứa quá nhiều file")
        if sum(member.file_size for member in members) > MAX_UNCOMPRESSED:
            raise ConversionError("Dung lượng giải nén vượt 300 MB")
        total_written = 0
        for member in members:
            path = PurePosixPath(member.filename.replace("\\", "/"))
            if path.is_absolute() or ".." in path.parts:
                raise ConversionError("ZIP chứa đường dẫn không an toàn")
            unix_mode = member.external_attr >> 16
            if unix_mode and stat.S_ISLNK(unix_mode):
                raise ConversionError("ZIP không được chứa symbolic link")
            if member.is_dir():
                continue
            if member.file_size > MAX_SINGLE_FILE:
                raise ConversionError("ZIP chứa file đơn vượt 60 MB")
            if member.file_size > 5 * 1024 * 1024 and member.compress_size:
                if member.file_size / member.compress_size > 500:
                    raise ConversionError("ZIP có tỷ lệ nén bất thường")
            target = destination.joinpath(*path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as src, target.open("wb") as dst:
                while chunk := src.read(1024 * 1024):
                    total_written += len(chunk)
                    if total_written > MAX_UNCOMPRESSED:
                        raise ConversionError("Dung lượng giải nén vượt 300 MB")
                    dst.write(chunk)


def _json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _strings(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


def _resolve_render_model(item_definition: dict, item_id: str) -> str | None:
    model = item_definition.get("model")
    if isinstance(model, dict) and isinstance(model.get("model"), str):
        return model["model"]
    for value in _strings(item_definition):
        if IDENTIFIER.match(value) and (":item/" in value or ":items/" in value):
            return value
    namespace, name = item_id.split(":", 1)
    return f"{namespace}:item/{name}"


def _model_path(root: Path, identifier: str) -> Path:
    namespace, name = identifier.split(":", 1)
    name = name.removeprefix("models/")
    return root / "assets" / namespace / "models" / f"{name}.json"


def _texture_path(root: Path, identifier: str) -> Path:
    namespace, name = identifier.split(":", 1)
    name = name.removeprefix("textures/")
    return root / "assets" / namespace / "textures" / f"{name}.png"


def _discover_items(root: Path, warnings: list[str]) -> list[ItemModel]:
    found: list[ItemModel] = []
    assets = root / "assets"
    if not assets.is_dir():
        raise ConversionError("Không tìm thấy thư mục assets trong resource pack")
    for namespace_dir in assets.iterdir():
        if not namespace_dir.is_dir() or namespace_dir.name == "minecraft":
            continue
        item_dir = namespace_dir / "items"
        if not item_dir.is_dir():
            continue
        for definition_path in item_dir.rglob("*.json"):
            relative = definition_path.relative_to(item_dir).with_suffix("").as_posix()
            item_id = f"{namespace_dir.name}:{relative}"
            definition = _json(definition_path)
            if not definition:
                warnings.append(f"SKIP {item_id}: item definition JSON không hợp lệ")
                continue
            render_id = _resolve_render_model(definition, item_id)
            model = _json(_model_path(root, render_id)) if render_id else None
            textures = model.get("textures", {}) if model else {}
            texture_id = textures.get("layer0") or textures.get("all") or textures.get("texture")
            if not isinstance(texture_id, str) or not IDENTIFIER.match(texture_id):
                warnings.append(f"SKIP {item_id}: chỉ hỗ trợ model phẳng có texture layer0/all")
                continue
            texture = _texture_path(root, texture_id)
            if not texture.is_file():
                warnings.append(f"SKIP {item_id}: thiếu texture {texture_id}.png")
                continue
            parent = str(model.get("parent", "")) if model else ""
            found.append(ItemModel(item_id, texture_id, texture, "handheld" in parent))
    return found


def _plain_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = re.sub(r"<[^>]+>", "", value)
    value = re.sub(r"§.", "", value)
    return value.strip() or None


def _read_oraxen_configs(oraxen_root: Path, warnings: list[str]) -> dict[str, OraxenItemConfig]:
    item_root = oraxen_root / "items"
    if not item_root.is_dir():
        warnings.append("CONFIG: không tìm thấy Oraxen/items; Material có thể phải dùng fallback")
        return {}
    result: dict[str, OraxenItemConfig] = {}
    paths = [*item_root.rglob("*.yml"), *item_root.rglob("*.yaml")]
    for path in paths:
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            warnings.append(f"CONFIG SKIP {path.name}: YAML không hợp lệ ({exc.__class__.__name__})")
            continue
        if not isinstance(document, dict):
            continue
        for raw_id, value in document.items():
            if not isinstance(raw_id, str) or not isinstance(value, dict):
                continue
            item_id = raw_id.lower()
            material = str(value.get("material", value.get("Material", "paper"))).lower()
            material = material.removeprefix("minecraft:")
            pack = value.get("Pack", value.get("pack", {}))
            pack = pack if isinstance(pack, dict) else {}
            model_hint = pack.get("model") or pack.get("model_path")
            display_name = _plain_name(
                value.get("displayname", value.get("display_name", value.get("name")))
            )
            result[item_id] = OraxenItemConfig(
                item_id, f"minecraft:{material}", display_name,
                str(model_hint) if model_hint else None,
            )
    return result


def _find_combined_roots(extracted: Path) -> tuple[Path, Path | None]:
    candidates = [extracted, *[p for p in extracted.rglob("*") if p.is_dir()]]
    pack_roots = [p for p in candidates if (p / "assets").is_dir()]
    if not pack_roots:
        raise ConversionError("Không tìm thấy resource-pack/assets trong ZIP")
    pack_roots.sort(key=lambda p: (p.name.lower() != "resource-pack", len(p.parts)))
    pack_root = pack_roots[0]
    config_roots = [p for p in candidates if p.name.lower() == "oraxen" and (p / "items").is_dir()]
    config_roots.sort(key=lambda p: len(p.parts))
    return pack_root, config_roots[0] if config_roots else None


def _infer_materials(root: Path, item_ids: set[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    vanilla_items = root / "assets" / "minecraft" / "items"
    if not vanilla_items.is_dir():
        return result
    for path in vanilla_items.rglob("*.json"):
        data = _json(path)
        if not data:
            continue
        material = f"minecraft:{path.relative_to(vanilla_items).with_suffix('').as_posix()}"
        values = set(_strings(data))
        for item_id in item_ids.intersection(values):
            result.setdefault(item_id, material)
    return result


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def convert_oraxen_pack(source_zip: Path, output_zip: Path, fallback_material: str) -> ConversionSummary:
    fallback_material = fallback_material.lower().strip()
    if not SAFE_MATERIAL.fullmatch(fallback_material):
        raise ConversionError("fallback_material không hợp lệ")
    if ":" not in fallback_material:
        fallback_material = "minecraft:" + fallback_material

    workspace = output_zip.parent / "conversion"
    extracted_root = workspace / "input"
    result_root = workspace / "result"
    bedrock = result_root / "bedrock-pack"
    extracted_root.mkdir(parents=True, exist_ok=True)
    bedrock.mkdir(parents=True, exist_ok=True)
    _safe_extract(source_zip, extracted_root)
    warnings: list[str] = []
    java_root, oraxen_root = _find_combined_roots(extracted_root)
    configs = _read_oraxen_configs(oraxen_root, warnings) if oraxen_root else {}
    if oraxen_root is None:
        warnings.append("CONFIG: ZIP không có thư mục Oraxen/items")
    items = _discover_items(java_root, warnings)
    if not items:
        raise ConversionError("Không tìm thấy item Oraxen 1.21.4+ có thể chuyển")
    inferred = _infer_materials(java_root, {item.identifier for item in items})

    texture_data: dict[str, object] = {}
    mappings: dict[str, list[dict]] = {}
    for item in items:
        namespace, name = item.identifier.split(":", 1)
        safe_name = name.replace("/", "_")
        bedrock_id = f"{namespace}:{safe_name}"
        icon = f"{namespace}.{safe_name}"
        target_texture = bedrock / "textures" / "items" / namespace / f"{safe_name}.png"
        target_texture.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item.source_texture, target_texture)
        texture_data[icon] = {"textures": f"textures/items/{namespace}/{safe_name}"}
        config_key = item.identifier.split(":", 1)[1].rsplit("/", 1)[-1]
        config = configs.get(config_key)
        material = config.material if config else inferred.get(item.identifier, fallback_material)
        if config is None and item.identifier not in inferred:
            warnings.append(
                f"FALLBACK {item.identifier}: dùng {material}; phải trùng Material trong cấu hình Oraxen"
            )
        definition = {
            "type": "definition",
            "model": item.identifier,
            "bedrock_identifier": bedrock_id,
            "display_name": (config.display_name if config else None) or name.rsplit("/", 1)[-1].replace("_", " ").title(),
            "bedrock_options": {"icon": icon, "display_handheld": item.handheld},
        }
        mappings.setdefault(material, []).append(definition)

    header_uuid, module_uuid = str(uuid.uuid4()), str(uuid.uuid4())
    _write_json(bedrock / "manifest.json", {
        "format_version": 2,
        "header": {"name": "Oraxen Geyser Pack", "description": "Generated by Oraxen Geyser Discord bot", "uuid": header_uuid, "version": [1, 0, 0], "min_engine_version": [1, 21, 0]},
        "modules": [{"type": "resources", "uuid": module_uuid, "version": [1, 0, 0]}],
    })
    _write_json(bedrock / "textures" / "item_texture.json", {
        "resource_pack_name": "oraxen_geyser", "texture_name": "atlas.items", "texture_data": texture_data
    })
    _write_json(result_root / "geyser-mappings.json", {"format_version": 2, "items": mappings})
    report = [
        "Oraxen -> Geyser conversion report", f"Converted items: {len(items)}",
        f"Oraxen item configs: {len(configs)}", f"Warnings: {len(warnings)}", "",
        "Supported exactly: flat item icons, Oraxen Material and display name.",
        "Not equivalent yet: 3D models, blocks/furniture, equipped armor, fonts, shaders, sounds and animations.",
        "Every FALLBACK material must match the Material configured for that Oraxen item.", "", *warnings,
    ]
    (result_root / "conversion-report.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
    shutil.make_archive(str(output_zip.with_suffix("")), "zip", result_root)
    return ConversionSummary(len(items), len(warnings))
