"""Discover and select feature cogs without starting Discord."""

from __future__ import annotations

import ast
import importlib
import logging
import sys
from pathlib import Path

COGS_DIR = Path(__file__).resolve().parent / "cogs"
COGS_PACKAGE = "cogs"

log = logging.getLogger(__name__)

# Other cogs already bound these names. A restart applies changes here.
SHARED_ROOTS = frozenset({"announce", "notice", "guild", "config", "loader", "nyatify"})
# Admin imports other features to hide their commands. Ownership stays with the feature.
_NOT_FEATURE_OWNERS = frozenset({"admin"})


# Module stem -> short name shown in slash/prefix commands.
# Unlisted cogs use the filename (without .py).
COG_PUBLIC_NAMES = {
    "voice_notify": "voice",
    "news_notify": "news",
    "osa_musify": "music",
}
_STEM_FROM_PUBLIC = {public: stem for stem, public in COG_PUBLIC_NAMES.items()}


def available_cog_stems() -> list[str]:
    return sorted(
        path.stem
        for path in COGS_DIR.glob("*.py")
        if not path.name.startswith("_")
    )


def normalize_cog_name(name: str) -> str:
    name = name.strip()
    prefix = f"{COGS_PACKAGE}."
    if name.startswith(prefix):
        return name[len(prefix) :]
    return name


def public_cog_name(stem: str) -> str:
    key = normalize_cog_name(stem)
    return COG_PUBLIC_NAMES.get(key, key)


def resolve_cog_stem(name: str) -> str:
    """Turn a command name or alias into the Python module stem."""
    key = normalize_cog_name(name)
    if key in COG_PUBLIC_NAMES:
        return key
    return _STEM_FROM_PUBLIC.get(key, key)


def public_cog_names() -> list[str]:
    return [public_cog_name(stem) for stem in available_cog_stems()]


def extension_name(name: str) -> str:
    return f"{COGS_PACKAGE}.{resolve_cog_stem(name)}"


def selected_extensions(enabled: list[str] | None) -> list[str]:
    """Return extension names to load.

    enabled=None or [] means every available cog.
    Otherwise only the named cogs are returned, so one cog can run alone.
    """
    available = available_cog_stems()
    chosen = available
    if enabled:
        wanted = {resolve_cog_stem(name) for name in enabled if name.strip()}
        unknown = wanted - set(available)
        if unknown:
            log.warning(
                "Unknown cog(s) in ENABLED_COGS: %s", ", ".join(sorted(unknown))
            )
        chosen = [stem for stem in available if stem in wanted]
        skipped = [stem for stem in available if stem not in wanted]
        if skipped:
            log.info("Isolation mode: skipping %s", ", ".join(skipped))
    return [f"{COGS_PACKAGE}.{stem}" for stem in chosen]


def cog_feature_packages(name: str) -> tuple[str, ...]:
    """Project packages that belong to this cog and no other feature cog."""
    stem = resolve_cog_stem(name)
    owners = _feature_owners()
    return tuple(sorted(package for package, owner in owners.items() if owner == stem))


def reload_cog_modules(name: str) -> list[str]:
    """Reload the cog's feature modules so a later cog reload sees new code.

    discord.py reloads the cog file only. A name imported from `music.commands.player`
    would keep the copy loaded at startup.
    """
    packages = cog_feature_packages(name)
    reloaded = reload_packages(packages)
    if reloaded:
        log.info(
            "Reloaded %s module(s) for %s (%s)",
            len(reloaded),
            extension_name(name),
            ", ".join(packages),
        )
    return reloaded


def reload_packages(packages: tuple[str, ...]) -> list[str]:
    """Reload loaded modules under these packages, dependencies first.

    Modules that import each other reload a second time so `from x import y`
    binds the new object.
    """
    loading = _loaded_feature_modules(packages)
    if not loading:
        return []
    once, cyclic = _reload_order(loading)
    order = [*once, *cyclic]
    saved = {name: dict(sys.modules[name].__dict__) for name in order}
    importlib.invalidate_caches()
    try:
        for module_name in once:
            _reload_module(module_name)
        # A second pass rebinds `from x import y` inside a real import cycle.
        for module_name in cyclic:
            _reload_module(module_name)
        for module_name in cyclic:
            _reload_module(module_name)
    except Exception:
        _restore_modules(saved)
        raise
    return order


def _reload_module(module_name: str) -> None:
    module = sys.modules[module_name]
    # Bytecode is stamped by the second. A save in that same second would reload
    # the previous copy unless the cached file is removed first.
    cached = getattr(module, "__cached__", None)
    if isinstance(cached, str):
        try:
            Path(cached).unlink(missing_ok=True)
        except OSError:
            pass
    importlib.reload(module)


def _restore_modules(saved: dict[str, dict[str, object]]) -> None:
    """Put modules back so a failed reload does not leave them half-executed."""
    for name, snapshot in saved.items():
        module = sys.modules.get(name)
        if module is None:
            continue
        module.__dict__.clear()
        module.__dict__.update(snapshot)


def _feature_owners() -> dict[str, str]:
    found: dict[str, set[str]] = {}
    for stem in available_cog_stems():
        if stem in _NOT_FEATURE_OWNERS:
            continue
        for root in _file_import_roots(COGS_DIR / f"{stem}.py"):
            if root in SHARED_ROOTS or root == COGS_PACKAGE:
                continue
            if not _is_project_module(root):
                continue
            found.setdefault(root, set()).add(stem)
    return {
        package: next(iter(stems))
        for package, stems in found.items()
        if len(stems) == 1
    }


def _file_import_roots(path: Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return set()
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.partition(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.partition(".")[0])
    return roots


def _is_project_module(root: str) -> bool:
    base = COGS_DIR.parent
    return (base / f"{root}.py").is_file() or (base / root).is_dir()


def _loaded_feature_modules(packages: tuple[str, ...]) -> set[str]:
    if not packages:
        return set()
    found: set[str] = set()
    for name, module in list(sys.modules.items()):
        if module is None or not _in_packages(name, packages):
            continue
        file = getattr(module, "__file__", None)
        if not isinstance(file, str) or not file.endswith(".py"):
            continue
        loader = getattr(getattr(module, "__spec__", None), "loader", None)
        if not hasattr(loader, "exec_module"):
            continue
        found.add(name)
    return found


def _in_packages(name: str, packages: tuple[str, ...]) -> bool:
    return any(name == package or name.startswith(f"{package}.") for package in packages)


def _reload_order(loading: set[str]) -> tuple[list[str], list[str]]:
    deps = {name: _dependencies(name, loading) for name in loading}
    ordered: list[str] = []
    remaining = set(loading)
    while remaining:
        ready = sorted(name for name in remaining if deps[name].isdisjoint(remaining))
        if not ready:
            return ordered, sorted(remaining)
        for name in ready:
            ordered.append(name)
            remaining.remove(name)
    return ordered, []


def _dependencies(module_name: str, loading: set[str]) -> set[str]:
    needed: set[str] = set()
    for target in _import_targets(module_name):
        name = target
        while name:
            if name in loading and name != module_name:
                needed.add(name)
                break
            name = name.rpartition(".")[0]
    return needed


def _import_targets(module_name: str) -> set[str]:
    module = sys.modules.get(module_name)
    path = getattr(module, "__file__", None)
    if not isinstance(path, str):
        return set()
    try:
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        base = _import_from_base(module_name, node)
        if not base:
            continue
        if node.module:
            found.add(base)
        for alias in node.names:
            found.add(f"{base}.{alias.name}")
    return found


def _import_from_base(module_name: str, node: ast.ImportFrom) -> str | None:
    if node.level == 0:
        return node.module
    parts = module_name.split(".")
    module = sys.modules.get(module_name)
    if module is not None and getattr(module, "__path__", None) is None:
        parts = parts[:-1]
    keep = len(parts) - (node.level - 1)
    if keep < 0:
        return None
    base = ".".join(parts[:keep])
    if node.module:
        return f"{base}.{node.module}" if base else node.module
    return base


def required_intent_names(enabled: list[str] | None) -> tuple[str, ...]:
    """Union of REQUIRED_INTENTS from the cogs that will load."""
    names: list[str] = []
    seen: set[str] = set()
    for extension in selected_extensions(enabled):
        module = importlib.import_module(extension)
        for name in getattr(module, "REQUIRED_INTENTS", ()):
            if name in seen:
                continue
            seen.add(name)
            names.append(name)
    return tuple(names)
