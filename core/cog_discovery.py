"""Discover feature cogs without importing disabled modules.

Recovery protection is explicit policy shared by boot and operator controls.
Stable bare IDs keep existing disabled_cogs values valid across file moves.
"""
from pathlib import Path


PROTECTED_COGS = {
    "control": "Provides configuration, feature enable/disable, and restart recovery.",
    "admin": "Provides initial ownership recovery and administrator management.",
}


def cog_name(value: str) -> str:
    """Normalize a bare ID or a current/historical qualified module name."""
    name = str(value).strip().lower()
    for prefix in ("cogs.optional.", "cogs.core.", "cogs."):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def protection_reason(name: str) -> str | None:
    """A recovery cog cannot be disabled through config or operator controls."""
    return PROTECTED_COGS.get(cog_name(name))


def list_cog_modules(config=None) -> list[str]:
    """Return discovered cogs, recovery first, optionally applying boot config.

    With no Config, include disabled cogs for operator listings. Only public
    Python modules directly in cogs/ are features; subfolders and underscore
    helpers are not auto-loaded. No feature module is imported to discover it.
    """
    directory = Path("cogs")
    if not directory.is_dir():
        return []
    disabled = {cog_name(name) for name in (config.get_global("disabled_cogs", []) or [])} if config is not None else set()
    names = [path.stem for path in directory.iterdir()
             if path.is_file() and path.suffix == ".py" and not path.name.startswith("_")]
    return [f"cogs.{name}" for name in sorted(names, key=lambda name: (name not in PROTECTED_COGS, name))
            if name not in disabled or name in PROTECTED_COGS]
