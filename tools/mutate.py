"""Mutation testing for the code where a quiet bug would hurt most.

    python tools/mutate.py                      # every target below
    python tools/mutate.py --only _fuzzy,retrace_matches

Each mutant is a copy of the project with one small change in one of the
functions below: a comparison flipped (< to <=, == to !=), "and" swapped for
"or", a "not" dropped, or a number nudged. The tests that cover that code are
run against it. A mutant the tests still pass is a "survivor": a bug no test
would notice. Run on a copy, so the working tree is never touched.
"""
import ast
import copy
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable

# file, function names, unittest -k patterns for the tests that cover them
TARGETS = [
    ("campus.py", {"find_places", "_fuzzy", "resolve_place", "split_search"},
     ["test_campus", "place", "search", "retrace"]),
    ("app.py", {"retrace_matches"}, ["retrace"]),
    ("app.py", {"desk_handover", "issue_handover_code", "code_is_live", "reissue_code"},
     ["handover", "code", "collect", "desk", "reissue"]),
    ("app.py", {"escalate_unclaimed_valuables"}, ["escalat", "valuable", "office"]),
    ("app.py", {"set_place", "search_filter"}, ["place", "search", "picker"]),
]

COMPARE_SWAPS = {ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt,
                 ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.In: ast.NotIn, ast.NotIn: ast.In}


def mutation_sites(tree, names):
    """(description, function that applies it to a copy of the tree) for every site."""
    sites = []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)) or func.name not in names:
            continue
        for node in ast.walk(func):
            path = (func.name, node.lineno if hasattr(node, "lineno") else 0)
            if isinstance(node, ast.Compare):
                for i, op in enumerate(node.ops):
                    if type(op) in COMPARE_SWAPS:
                        sites.append((f"{path[0]}:{path[1]} {type(op).__name__} -> {COMPARE_SWAPS[type(op)].__name__}",
                                      node, ("compare", i)))
            elif isinstance(node, ast.BoolOp):
                sites.append((f"{path[0]}:{path[1]} {type(node.op).__name__} -> "
                              f"{'Or' if isinstance(node.op, ast.And) else 'And'}", node, ("boolop",)))
            elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
                sites.append((f"{path[0]}:{path[1]} drop not", node, ("not",)))
            elif isinstance(node, ast.Constant) and type(node.value) in (int, float) and not isinstance(node.value, bool):
                sites.append((f"{path[0]}:{path[1]} {node.value!r} -> {node.value + 1!r}", node, ("number",)))
    return sites


def apply(tree, target, how):
    """A copy of the tree with the node equal to `target` mutated."""
    mutated = copy.deepcopy(tree)
    originals = list(ast.walk(tree))
    index = next(i for i, node in enumerate(originals) if node is target)
    node = list(ast.walk(mutated))[index]
    kind = how[0]
    if kind == "compare":
        node.ops[how[1]] = COMPARE_SWAPS[type(node.ops[how[1]])]()
    elif kind == "boolop":
        node.op = ast.Or() if isinstance(node.op, ast.And) else ast.And()
    elif kind == "not":
        replacement = node.operand
        for parent in ast.walk(mutated):
            for field, value in ast.iter_fields(parent):
                if value is node:
                    setattr(parent, field, replacement)
                elif isinstance(value, list) and node in value:
                    value[value.index(node)] = replacement
    elif kind == "number":
        node.value = node.value + 1
    return mutated


def run_tests(work, patterns):
    command = [PYTHON, "-m", "unittest", "discover", "-s", "tests"]
    for pattern in patterns:
        command += ["-k", pattern]
    done = subprocess.run(command, cwd=work, capture_output=True, text=True, timeout=600)
    return done.returncode == 0


def main():
    only = set(sys.argv[sys.argv.index("--only") + 1].split(",")) if "--only" in sys.argv else None
    work = Path(tempfile.mkdtemp(prefix="classfind-mutants-"))
    shutil.copytree(ROOT, work, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git", ".v", "__pycache__", "*.db"))
    total = killed = 0
    survivors = []
    started = time.monotonic()
    try:
        for filename, names, patterns in TARGETS:
            if only:
                names = names & only
                if not names:
                    continue
            source = (ROOT / filename).read_text(encoding="utf-8")
            tree = ast.parse(source)
            if not run_tests(work, patterns):
                print(f"Tests for {sorted(names)} fail before any mutation; fix them first.")
                return 2
            for description, node, how in mutation_sites(tree, names):
                (work / filename).write_text(ast.unparse(apply(tree, node, how)), encoding="utf-8")
                caught = not run_tests(work, patterns)
                total += 1
                killed += caught
                if not caught:
                    survivors.append(f"{filename} {description}")
                print(f"{'killed  ' if caught else 'SURVIVED'} {filename} {description}", flush=True)
            (work / filename).write_text(source, encoding="utf-8")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    score = 100 * killed / total if total else 100
    print(f"\n{killed} of {total} mutants caught ({score:.0f}%) in {(time.monotonic() - started) / 60:.1f} min")
    if survivors:
        print("Survivors, each a change no test noticed:")
        for line in survivors:
            print("  " + line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
