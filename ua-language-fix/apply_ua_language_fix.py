"""Local, non-GitHub patcher. Applies to extracted current v2 source only."""
import argparse
import ast
import shutil
from datetime import datetime, timezone
from pathlib import Path

MARKER = "# UA_LANGUAGE_GUARD_20261025"
INSERT = "\n\n" + MARKER + "\nfrom app.ua_language_guard import protect_publish as _ua_protect_publish\npublish = _ua_protect_publish(publish, globals())\n"


def check(root: Path):
    main = root / "app" / "main.py"
    guard = root / "app" / "ua_language_guard.py"
    if not main.is_file() or not guard.is_file():
        raise RuntimeError("Required app/main.py or app/ua_language_guard.py missing; wrong source directory")
    content = main.read_text(encoding="utf-8")
    tree = ast.parse(content, filename=str(main))
    names = {node.name: node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    publish = names.get("publish")
    if not isinstance(publish, ast.AsyncFunctionDef) or not publish.args.args or publish.args.args[0].arg != "docid":
        raise RuntimeError("Source incompatible: expected async publish(docid, ...). No modifications made.")
    for required in ("route_of", "editor_config"):
        if not isinstance(names.get(required), (ast.FunctionDef, ast.AsyncFunctionDef)):
            raise RuntimeError(f"Source incompatible: {required}() not found. No modifications made.")
    if not any(isinstance(node, (ast.Assign, ast.AnnAssign)) and
               any(isinstance(t, ast.Name) and t.id == "db" for t in getattr(node, 'targets', []))
               for node in tree.body):
        # db may be defined via a module-level typed assignment; warn only.
        print("WARNING: db global not found by simple AST scan; ensure module defines db")
    if any(isinstance(node, ast.If) and isinstance(node.test, ast.Compare) and "__name__" in ast.unparse(node.test)
           for node in tree.body):
        raise RuntimeError("Source has __main__ dispatch; manual integration required. No modifications made.")
    return main, content


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    target, content = check(root)
    if MARKER in content:
        print("Language guard already installed; no changes")
        return
    if args.apply:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = target.with_name("main.py.pre-ua-guard-" + stamp + ".bak")
        shutil.copy2(target, backup)
        candidate = content + INSERT
        ast.parse(candidate, filename=str(target))
        target.write_text(candidate, encoding="utf-8")
        print("Patch applied, backup:", backup)
    else:
        print("Compatibility OK: async publish(docid), editor_config(), route_of() found")
        print("No changes made. Use --apply after tests.")


if __name__ == "__main__":
    main()
