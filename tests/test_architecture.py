"""Enforce feature ownership without importing cogs or deployment configuration."""
import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COG_BASE = "discord.ext.commands.Cog"
BEHAVIORAL_OP = "core.ops.op"
FEATURE_DECORATORS = {
    "discord.ext.commands.command",
    "discord.ext.commands.group",
    "discord.ext.commands.hybrid_command",
    "discord.ext.commands.hybrid_group",
    "discord.ext.commands.Cog.listener",
    "discord.app_commands.command",
    "discord.app_commands.context_menu",
    "discord.ext.tasks.loop",
}


def _runtime_sources():
    yield ROOT / "bot.py"
    for directory in ("core", "cogs"):
        yield from sorted((ROOT / directory).rglob("*.py"))


def _imports(tree, path):
    """Resolve ordinary import aliases, including package-relative imports."""
    aliases = {}
    imports = []
    package = list(path.relative_to(ROOT).parts[:-1])
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                aliases[item.asname or item.name.split(".")[0]] = (
                    item.name if item.asname else item.name.split(".")[0])
                imports.append((node.lineno, item.name))
        elif isinstance(node, ast.ImportFrom):
            parts = package[:len(package) - node.level + 1] if node.level else []
            module = ".".join(parts + ([node.module] if node.module else []))
            imports.append((node.lineno, module))
            for item in node.names:
                aliases[item.asname or item.name] = f"{module}.{item.name}"
    return aliases, imports


def _symbol(node, aliases):
    if isinstance(node, ast.Call):
        return _symbol(node.func, aliases)
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        return f"{_symbol(node.value, aliases)}.{node.attr}"
    return ""


def _violations(source, path):
    tree = ast.parse(source, filename=str(path))
    aliases, imports = _imports(tree, path)
    in_cogs = path.relative_to(ROOT).parts[0] == "cogs"
    dependencies, declarations = [], []
    for line, module in imports:
        if module == "cogs" or module.startswith("cogs."):
            dependencies.append(f"{path.relative_to(ROOT)}:{line}: "
                                "reuse a core service instead of importing cogs")

    parents = {child: parent for parent in ast.walk(tree)
               for child in ast.iter_child_nodes(parent)}
    classes = [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
    # Include locally inherited Cog classes, without importing executable modules.
    cog_names = {COG_BASE}
    cog_classes = set()
    while True:
        found = {node for node in classes
                 if any(_symbol(base, aliases) in cog_names for base in node.bases)}
        if found <= cog_classes:
            break
        cog_classes |= found
        cog_names.update(node.name for node in found)

    def reject(node, reason):
        declarations.append(f"{path.relative_to(ROOT)}:{node.lineno}: {reason}")

    for node in ast.walk(tree):
        if node in cog_classes and not in_cogs:
            reject(node, "Cog classes belong in cogs/")
        for decorator in getattr(node, "decorator_list", ()):
            symbol = _symbol(decorator, aliases)
            if symbol == BEHAVIORAL_OP:
                if (not in_cogs or parents.get(node) not in cog_classes
                        or not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))):
                    reject(decorator, "behavioral @op belongs on an owning Cog method")
            elif symbol in FEATURE_DECORATORS and not in_cogs:
                reject(decorator, "Discord feature entrypoints belong in cogs/")
    return dependencies, declarations


def test_services_and_cogs_do_not_depend_on_cogs():
    violations = []
    for path in _runtime_sources():
        # bot.py is the composition root and may wire cogs together.
        if path.name == "bot.py" and path.parent == ROOT:
            continue
        dependencies, _ = _violations(path.read_text(encoding="utf-8"), path)
        violations.extend(dependencies)
    assert not violations, "\n".join(violations)


def test_feature_declarations_belong_to_cogs():
    violations = []
    for path in _runtime_sources():
        _, declarations = _violations(path.read_text(encoding="utf-8"), path)
        violations.extend(declarations)
    assert not violations, "\n".join(violations)
