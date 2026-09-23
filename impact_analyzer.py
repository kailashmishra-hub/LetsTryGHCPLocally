from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable


SOURCE_SUFFIXES = {".java", ".kt", ".groovy", ".scala", ".py", ".js", ".ts", ".tsx"}
DEFAULT_IMPACT_ROOTS = ("src/main/java/", "src/test/java/")
IGNORED_PARTS = {".git", ".idea", ".venv", "__pycache__", "node_modules", "target", "build", "dist"}
STEP_ANNOTATIONS = {"Given", "When", "Then", "And", "But"}


@dataclass
class ChangedRange:
    old_start: int | None
    old_end: int | None
    new_start: int | None
    new_end: int | None
    change_kind: str
    removed_lines: list[str] = field(default_factory=list)
    added_lines: list[str] = field(default_factory=list)


@dataclass
class SymbolInfo:
    symbol_type: str
    name: str
    qualified_name: str
    file_path: str
    language: str
    package_name: str | None
    class_name: str | None
    signature: str | None
    start_line: int
    end_line: int
    annotations: list[str] = field(default_factory=list)
    changed_lines: list[int] = field(default_factory=list)
    changed_ranges: list[ChangedRange] = field(default_factory=list)
    snippet: str | None = None


@dataclass
class ChangedFile:
    status: str
    path: str
    old_path: str | None
    language: str
    diff: str
    structured_hunks: list[ChangedRange]
    changed_symbols: list[SymbolInfo]
    unmapped_changes: list[ChangedRange] = field(default_factory=list)


@dataclass
class StepDefinition:
    file_path: str
    package_name: str | None
    class_name: str | None
    method_name: str
    qualified_name: str
    annotation_type: str
    pattern: str
    line: int
    method_start_line: int | None = None
    method_end_line: int | None = None


@dataclass
class Scenario:
    feature_path: str
    feature_name: str | None
    scenario_name: str
    scenario_type: str
    line: int
    tags: list[str]
    steps: list[dict]


@dataclass
class ScenarioCandidate:
    feature_path: str
    feature_name: str | None
    scenario_name: str
    scenario_line: int
    tags: list[str]
    matched_step: str
    matched_step_line: int
    matched_step_source: str
    step_definition: str
    confidence: str
    reason: str
    trace_path: list[str]
    risk_score: int = 0
    risk_level: str = "unscored"
    risk_factors: dict = field(default_factory=dict)


@dataclass
class TraceInput:
    schema_version: str
    repo: str
    base_ref: str
    base_sha: str
    target_ref: str
    target_sha: str
    changed_files: list[ChangedFile]
    impacted_step_definitions: list[StepDefinition]
    scenario_candidates: list[ScenarioCandidate]
    unresolved_symbols: list[dict]
    repository_index_summary: dict
    repository_index: dict | None = None


def run_git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    if check and result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Git command failed")
    return result.stdout


def validate_repo(repo: Path) -> Path:
    repo = repo.expanduser().resolve()
    if not repo.exists():
        raise RuntimeError(f"Repository does not exist: {repo}")
    return Path(run_git(repo, "rev-parse", "--show-toplevel").strip()).resolve()


def default_base(repo: Path) -> str:
    try:
        head_ref = run_git(repo, "symbolic-ref", "refs/remotes/origin/HEAD").strip()
        if head_ref:
            return head_ref.replace("refs/remotes/", "")
    except Exception:
        pass

    for branch in ("origin/master", "origin/main", "origin/develop", "master", "main", "develop"):
        try:
            run_git(repo, "rev-parse", "--verify", branch)
            return branch
        except Exception:
            pass
    raise RuntimeError("Unable to detect default branch. Please supply --base.")


def parse_name_status(output: str) -> list[tuple[str, str | None, str]]:
    changes = []
    for line in output.splitlines():
        cols = line.split("\t")
        if len(cols) < 2:
            continue
        status = cols[0][0]
        if status in {"R", "C"} and len(cols) >= 3:
            changes.append((status, cols[1], cols[2]))
        else:
            changes.append((status, None, cols[1]))
    return changes


def language_for(path: str) -> str:
    suffix = Path(path).suffix.lower()
    return {
        ".java": "java",
        ".kt": "kotlin",
        ".groovy": "groovy",
        ".scala": "scala",
        ".py": "python",
        ".js": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript-react",
    }.get(suffix, "unknown")


def should_ignore(path: str, include_all_source: bool = False) -> bool:
    p = Path(path)
    normalized = path.replace("\\", "/")
    if p.suffix.lower() not in SOURCE_SUFFIXES:
        return True
    if any(part in IGNORED_PARTS for part in p.parts):
        return True
    if include_all_source:
        return False
    return not normalized.startswith(DEFAULT_IMPACT_ROOTS)


def read_ref_file(repo: Path, ref: str, path: str) -> str:
    return run_git(repo, "show", f"{ref}:{path}", check=False)


def read_target_file(repo: Path, target_ref: str, path: str) -> str:
    if target_ref in {"", "WORKTREE", "worktree"}:
        full_path = repo / path
        return full_path.read_text(encoding="utf-8", errors="replace") if full_path.exists() else ""
    return read_ref_file(repo, target_ref, path)


def split_changed_ranges(diff_text: str) -> list[ChangedRange]:
    ranges: list[ChangedRange] = []
    old_line: int | None = None
    new_line: int | None = None
    pending_removed: list[tuple[int, str]] = []
    pending_added: list[tuple[int, str]] = []

    def flush() -> None:
        nonlocal pending_removed, pending_added
        if not pending_removed and not pending_added:
            return
        old_nums = [n for n, _ in pending_removed]
        new_nums = [n for n, _ in pending_added]
        if pending_removed and pending_added:
            kind = "modified"
        elif pending_removed:
            kind = "deleted"
        else:
            kind = "added"
        ranges.append(
            ChangedRange(
                old_start=min(old_nums) if old_nums else None,
                old_end=max(old_nums) if old_nums else None,
                new_start=min(new_nums) if new_nums else None,
                new_end=max(new_nums) if new_nums else None,
                change_kind=kind,
                removed_lines=[text for _, text in pending_removed],
                added_lines=[text for _, text in pending_added],
            )
        )
        pending_removed = []
        pending_added = []

    for line in diff_text.splitlines():
        hunk = re.match(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if hunk:
            flush()
            old_line = int(hunk.group(1))
            new_line = int(hunk.group(2))
            continue
        if old_line is None or new_line is None:
            continue
        if line.startswith("\\"):
            continue
        if line.startswith("-"):
            pending_removed.append((old_line, line[1:]))
            old_line += 1
        elif line.startswith("+"):
            pending_added.append((new_line, line[1:]))
            new_line += 1
        elif line.startswith(" "):
            flush()
            old_line += 1
            new_line += 1
        else:
            flush()
    flush()
    return ranges


def changed_new_lines(ranges: Iterable[ChangedRange]) -> list[int]:
    lines = set()
    for item in ranges:
        if item.new_start is None or item.new_end is None:
            continue
        lines.update(range(item.new_start, item.new_end + 1))
    return sorted(lines)


def changed_old_lines(ranges: Iterable[ChangedRange]) -> list[int]:
    lines = set()
    for item in ranges:
        if item.old_start is None or item.old_end is None:
            continue
        lines.update(range(item.old_start, item.old_end + 1))
    return sorted(lines)


def extract_package(content: str) -> str | None:
    match = re.search(r"^\s*package\s+([\w.]+)\s*;", content, flags=re.MULTILINE)
    return match.group(1) if match else None


def extract_primary_class(content: str) -> str | None:
    match = re.search(r"\b(?:class|interface|enum|record)\s+(\w+)\b", content)
    return match.group(1) if match else None


def strip_literals_for_braces(line: str) -> str:
    line = re.sub(r'"(?:\\.|[^"\\])*"', '""', line)
    line = re.sub(r"'(?:\\.|[^'\\])*'", "''", line)
    line = re.sub(r"//.*", "", line)
    return line


def java_method_name(signature: str, class_name: str | None) -> str | None:
    cleaned = " ".join(signature.strip().split())
    if not cleaned or "=" in cleaned:
        return None
    match = re.search(
        r"(?:public|private|protected|static|final|native|synchronized|abstract|default|strictfp|\s)+"
        r"(?:<[^>]+>\s*)?(?:[\w.$<>\[\], ?]+\s+)?(\w+)\s*\([^;{}]*\)\s*(?:throws\s+[^{]+)?$",
        cleaned,
    )
    if not match:
        return None
    name = match.group(1)
    blocked = {"if", "for", "while", "switch", "catch", "return", "new", "class", "interface", "enum", "record"}
    if name in blocked:
        return None
    if class_name and name == class_name:
        return name
    return name


def extract_java_symbols(content: str, file_path: str) -> list[SymbolInfo]:
    package_name = extract_package(content)
    class_name = extract_primary_class(content)
    language = language_for(file_path)
    lines = content.splitlines()
    symbols: list[SymbolInfo] = []
    annotations: list[str] = []
    signature_lines: list[str] = []
    signature_start: int | None = None
    active: dict | None = None
    brace_depth = 0

    for line_no, line in enumerate(lines, 1):
        stripped = line.strip()
        code_line = strip_literals_for_braces(line)

        if active is None:
            if stripped.startswith("@"):
                annotations.append(stripped)
                continue
            if not stripped or stripped.startswith("//") or stripped.startswith("*"):
                continue

            signature_lines.append(stripped)
            signature_start = signature_start or line_no
            signature_text = " ".join(signature_lines)

            if "{" in stripped:
                before_brace = signature_text.split("{", 1)[0].strip()
                name = java_method_name(before_brace, class_name)
                if name:
                    qualified_parts = [part for part in (package_name, class_name) if part]
                    owner = ".".join(qualified_parts) if qualified_parts else Path(file_path).stem
                    active = {
                        "name": name,
                        "start": signature_start,
                        "signature": before_brace,
                        "annotations": annotations[:],
                        "base_depth": brace_depth,
                        "owner": owner,
                    }
                signature_lines = []
                signature_start = None
                annotations = []
            elif stripped.endswith(";"):
                signature_lines = []
                signature_start = None
                annotations = []

        brace_depth += code_line.count("{") - code_line.count("}")

        if active is not None and brace_depth <= active["base_depth"]:
            name = active["name"]
            owner = active["owner"]
            symbols.append(
                SymbolInfo(
                    symbol_type="method",
                    name=name,
                    qualified_name=f"{owner}#{name}",
                    file_path=file_path,
                    language=language,
                    package_name=package_name,
                    class_name=class_name,
                    signature=active["signature"],
                    start_line=active["start"],
                    end_line=line_no,
                    annotations=active["annotations"],
                )
            )
            active = None

    return symbols


def extract_python_symbols(content: str, file_path: str) -> list[SymbolInfo]:
    symbols: list[SymbolInfo] = []
    lines = content.splitlines()
    stack: list[tuple[int, str, int]] = []
    for line_no, line in enumerate(lines, 1):
        match = re.match(r"^(\s*)(?:async\s+def|def)\s+(\w+)\s*\(", line)
        if not match:
            continue
        indent = len(match.group(1))
        name = match.group(2)
        while stack and stack[-1][0] >= indent:
            stack.pop()
        owner = ".".join(item[1] for item in stack)
        qualified = f"{Path(file_path).with_suffix('').as_posix().replace('/', '.')}#{owner + '.' if owner else ''}{name}"
        end_line = len(lines)
        for later_no in range(line_no + 1, len(lines) + 1):
            later = lines[later_no - 1]
            if later.strip() and len(later) - len(later.lstrip()) <= indent:
                end_line = later_no - 1
                break
        symbols.append(
            SymbolInfo(
                symbol_type="function",
                name=name,
                qualified_name=qualified,
                file_path=file_path,
                language="python",
                package_name=None,
                class_name=owner or None,
                signature=line.strip(),
                start_line=line_no,
                end_line=end_line,
            )
        )
    return symbols


def extract_symbols(content: str, file_path: str) -> list[SymbolInfo]:
    suffix = Path(file_path).suffix.lower()
    if suffix == ".java":
        return extract_java_symbols(content, file_path)
    if suffix == ".py":
        return extract_python_symbols(content, file_path)
    return []


def line_intersects(symbol: SymbolInfo, lines: Iterable[int]) -> list[int]:
    return sorted(line for line in lines if symbol.start_line <= line <= symbol.end_line)


def snippet_for(content: str, start: int, end: int, padding: int = 3) -> str:
    lines = content.splitlines()
    first = max(1, start - padding)
    last = min(len(lines), end + padding)
    return "\n".join(f"{idx}: {lines[idx - 1]}" for idx in range(first, last + 1))


def map_ranges_to_symbols(
    content: str,
    old_content: str,
    file_path: str,
    ranges: list[ChangedRange],
) -> tuple[list[SymbolInfo], list[ChangedRange]]:
    new_lines = changed_new_lines(ranges)
    old_lines = changed_old_lines(ranges)
    new_symbols = extract_symbols(content, file_path)
    old_symbols = extract_symbols(old_content, file_path) if old_content else []
    affected: dict[str, SymbolInfo] = {}
    mapped_ranges: set[int] = set()

    for symbol in new_symbols:
        hits = line_intersects(symbol, new_lines)
        symbol_ranges = [
            item for item in ranges
            if item.new_start is not None and item.new_end is not None
            and not (item.new_end < symbol.start_line or item.new_start > symbol.end_line)
        ]
        if hits or symbol_ranges:
            clone = SymbolInfo(**{**asdict(symbol), "changed_lines": hits, "changed_ranges": symbol_ranges})
            clone.snippet = snippet_for(content, clone.start_line, clone.end_line)
            affected[clone.qualified_name] = clone
            for item in symbol_ranges:
                mapped_ranges.add(id(item))

    for symbol in old_symbols:
        hits = line_intersects(symbol, old_lines)
        deletion_ranges = [
            item for item in ranges
            if item.change_kind == "deleted"
            and item.old_start is not None and item.old_end is not None
            and not (item.old_end < symbol.start_line or item.old_start > symbol.end_line)
        ]
        if hits or deletion_ranges:
            key = symbol.qualified_name
            if key not in affected:
                clone = SymbolInfo(**{**asdict(symbol), "changed_lines": [], "changed_ranges": deletion_ranges})
                clone.snippet = snippet_for(old_content, clone.start_line, clone.end_line)
                affected[key] = clone
            for item in deletion_ranges:
                mapped_ranges.add(id(item))

    unmapped = [item for item in ranges if id(item) not in mapped_ranges]
    return list(affected.values()), unmapped


def iter_files(repo: Path, suffixes: set[str]) -> Iterable[Path]:
    for path in repo.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in suffixes:
            continue
        rel = path.relative_to(repo)
        if any(part in IGNORED_PARTS for part in rel.parts):
            continue
        yield path


def extract_step_definitions(repo: Path) -> list[StepDefinition]:
    step_defs: list[StepDefinition] = []
    annotation_re = re.compile(r"@(?P<type>Given|When|Then|And|But)\s*\(\s*(?P<quote>\"|')(?P<pattern>.*?)(?P=quote)\s*\)")

    for path in iter_files(repo, {".java"}):
        rel = path.relative_to(repo).as_posix()
        content = path.read_text(encoding="utf-8", errors="replace")
        package_name = extract_package(content)
        class_name = extract_primary_class(content)
        symbols = extract_java_symbols(content, rel)
        lines = content.splitlines()

        for line_no, line in enumerate(lines, 1):
            match = annotation_re.search(line.strip())
            if not match:
                continue
            containing = next((s for s in symbols if s.start_line >= line_no and s.start_line <= line_no + 8), None)
            if containing is None:
                containing = next((s for s in symbols if s.start_line <= line_no <= s.end_line), None)
            method_name = containing.name if containing else "unknown"
            qualified = containing.qualified_name if containing else f"{package_name or ''}.{class_name or Path(rel).stem}#{method_name}"
            step_defs.append(
                StepDefinition(
                    file_path=rel,
                    package_name=package_name,
                    class_name=class_name,
                    method_name=method_name,
                    qualified_name=qualified,
                    annotation_type=match.group("type"),
                    pattern=match.group("pattern"),
                    line=line_no,
                    method_start_line=containing.start_line if containing else None,
                    method_end_line=containing.end_line if containing else None,
                )
            )
    return step_defs


def parse_feature_files(repo: Path) -> list[Scenario]:
    scenarios: list[Scenario] = []
    step_re = re.compile(r"^\s*(Given|When|Then|And|But|\*)\s+(.+?)\s*$")

    for path in iter_files(repo, {".feature"}):
        rel = path.relative_to(repo).as_posix()
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        feature_name: str | None = None
        pending_tags: list[str] = []
        background_steps: list[dict] = []
        in_background = False
        current: Scenario | None = None

        for line_no, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("@"):
                pending_tags.extend(stripped.split())
                continue
            if stripped.lower().startswith("feature:"):
                feature_name = stripped.split(":", 1)[1].strip()
                continue
            if stripped.lower().startswith("background:"):
                in_background = True
                current = None
                continue
            scenario_match = re.match(r"^(Scenario(?: Outline)?):\s*(.+)$", stripped, flags=re.IGNORECASE)
            if scenario_match:
                in_background = False
                current = Scenario(
                    feature_path=rel,
                    feature_name=feature_name,
                    scenario_name=scenario_match.group(2).strip(),
                    scenario_type=scenario_match.group(1),
                    line=line_no,
                    tags=pending_tags,
                    steps=[dict(step) for step in background_steps],
                )
                scenarios.append(current)
                pending_tags = []
                continue
            step_match = step_re.match(line)
            if step_match and in_background:
                background_steps.append(
                    {
                        "keyword": step_match.group(1),
                        "text": step_match.group(2).strip(),
                        "line": line_no,
                        "source": "Background",
                    }
                )
                continue
            if step_match and current:
                current.steps.append(
                    {
                        "keyword": step_match.group(1),
                        "text": step_match.group(2).strip(),
                        "line": line_no,
                        "source": "Scenario",
                    }
                )
    return scenarios


def cucumber_pattern_to_regex(pattern: str) -> re.Pattern | None:
    raw = pattern.strip()
    if raw.startswith("^") or raw.endswith("$"):
        try:
            return re.compile(raw)
        except re.error:
            return None
    escaped = re.escape(raw)
    escaped = escaped.replace(r"\{string\}", r"(?:\"[^\"]*\"|'[^']*')")
    escaped = escaped.replace(r"\{int\}", r"-?\d+")
    escaped = escaped.replace(r"\{float\}", r"-?\d+(?:\.\d+)?")
    escaped = escaped.replace(r"\{word\}", r"\w+")
    escaped = re.sub(r"\\\{[^}]+\\\}", r"(?:.+?)", escaped)
    try:
        return re.compile(f"^{escaped}$", flags=re.IGNORECASE)
    except re.error:
        return None


def scenarios_for_step_definitions(step_defs: list[StepDefinition], scenarios: list[Scenario]) -> dict[str, list[ScenarioCandidate]]:
    compiled = [(step_def, cucumber_pattern_to_regex(step_def.pattern)) for step_def in step_defs]
    result: dict[str, list[ScenarioCandidate]] = {}

    for scenario in scenarios:
        for step in scenario.steps:
            step_text = step["text"]
            for step_def, regex in compiled:
                if regex is None or not regex.match(step_text):
                    continue
                candidate = ScenarioCandidate(
                    feature_path=scenario.feature_path,
                    feature_name=scenario.feature_name,
                    scenario_name=scenario.scenario_name,
                    scenario_line=scenario.line,
                    tags=scenario.tags,
                    matched_step=step_text,
                    matched_step_line=step["line"],
                    matched_step_source=step.get("source", "Scenario"),
                    step_definition=step_def.qualified_name,
                    confidence="high",
                    reason="Feature step matches step-definition annotation pattern.",
                    trace_path=[step_def.qualified_name, f"{scenario.feature_path}:{scenario.line}"],
                )
                result.setdefault(step_def.qualified_name, []).append(candidate)
    return result


def clone_candidate(candidate: ScenarioCandidate) -> ScenarioCandidate:
    return ScenarioCandidate(**asdict(candidate))


def dedupe_candidates(candidates: list[ScenarioCandidate]) -> list[ScenarioCandidate]:
    seen = set()
    unique: list[ScenarioCandidate] = []
    for item in candidates:
        key = (item.feature_path, item.scenario_line, item.matched_step_line, item.step_definition, item.matched_step_source)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def risk_level(score: int) -> str:
    if score >= 70:
        return "high"
    if score >= 40:
        return "medium"
    return "low"


def score_scenario_candidates(candidates: list[ScenarioCandidate], symbols: list[SymbolInfo]) -> list[ScenarioCandidate]:
    changed_symbol_count = len(symbols)
    changed_class_count = len({symbol.class_name for symbol in symbols if symbol.class_name})
    changed_line_count = sum(len(symbol.changed_lines) for symbol in symbols)
    changed_method_count = len({symbol.qualified_name for symbol in symbols if symbol.symbol_type in {"method", "function"}})

    grouped: dict[tuple[str, int], list[ScenarioCandidate]] = {}
    for candidate in candidates:
        grouped.setdefault((candidate.feature_path, candidate.scenario_line), []).append(candidate)

    for group in grouped.values():
        impacted_steps = sorted({item.step_definition for item in group})
        matched_sources = sorted({item.matched_step_source for item in group})
        direct_hits = sum(1 for item in group if item.confidence == "high" and item.reason.startswith("Changed method"))
        indirect_hits = sum(1 for item in group if item.confidence == "medium")
        background_hits = sum(1 for item in group if item.matched_step_source == "Background")

        score = 10
        score += min(50, 20 * len(impacted_steps))
        if direct_hits:
            score += 10
        if indirect_hits:
            score += 15
        if background_hits:
            score += 10
        score += min(10, 5 * changed_method_count)
        score += min(10, 5 * changed_class_count)
        score += min(10, changed_line_count)
        score = min(100, score)

        factors = {
            "impacted_step_definition_count": len(impacted_steps),
            "impacted_step_definitions": impacted_steps,
            "direct_step_definition_hits": direct_hits,
            "indirect_method_or_class_hits": indirect_hits,
            "background_step_hits": background_hits,
            "matched_step_sources": matched_sources,
            "changed_symbol_count": changed_symbol_count,
            "changed_method_or_function_count": changed_method_count,
            "changed_class_count": changed_class_count,
            "changed_line_count": changed_line_count,
            "scoring_model": (
                "Base 10 + impacted step definitions + direct/indirect trace strength + "
                "Background usage + changed method/class/line footprint, capped at 100."
            ),
        }

        for item in group:
            item.risk_score = score
            item.risk_level = risk_level(score)
            item.risk_factors = factors

    return candidates


def direct_step_definition_candidates(
    symbols: list[SymbolInfo],
    step_defs: list[StepDefinition],
    scenario_map: dict[str, list[ScenarioCandidate]],
) -> list[ScenarioCandidate]:
    candidates: list[ScenarioCandidate] = []
    step_defs_by_qname = {step_def.qualified_name: step_def for step_def in step_defs}

    for symbol in symbols:
        step_def = step_defs_by_qname.get(symbol.qualified_name)
        if step_def is None:
            step_def = next(
                (
                    item for item in step_defs
                    if item.file_path == symbol.file_path and item.method_name == symbol.name
                ),
                None,
            )
        if step_def is None:
            continue

        for item in scenario_map.get(step_def.qualified_name, []):
            copied = clone_candidate(item)
            copied.confidence = "high"
            copied.reason = "Changed method is the Cucumber step definition used by this scenario."
            copied.trace_path = [
                symbol.qualified_name,
                step_def.qualified_name,
                f"{item.feature_path}:{item.scenario_line}",
            ]
            candidates.append(copied)

    return candidates


def simple_call_candidates(symbols: list[SymbolInfo], step_defs: list[StepDefinition], scenario_map: dict[str, list[ScenarioCandidate]], repo: Path) -> list[ScenarioCandidate]:
    candidates: list[ScenarioCandidate] = []
    changed_method_names = {symbol.name for symbol in symbols}
    changed_class_names = {symbol.class_name for symbol in symbols if symbol.class_name}
    changed_qnames = {symbol.qualified_name for symbol in symbols}

    for step_def in step_defs:
        if step_def.qualified_name in changed_qnames:
            continue
        content = (repo / step_def.file_path).read_text(encoding="utf-8", errors="replace")
        body = content
        if step_def.method_start_line and step_def.method_end_line:
            lines = content.splitlines()
            body = "\n".join(lines[step_def.method_start_line - 1:step_def.method_end_line])
        method_hit = any(re.search(rf"\b{name}\s*\(", body) for name in changed_method_names)
        class_hit = any(re.search(rf"\b{name}\b", body) for name in changed_class_names)
        if not method_hit and not class_hit:
            continue
        for item in scenario_map.get(step_def.qualified_name, []):
            copied = clone_candidate(item)
            copied.confidence = "medium"
            copied.reason = "Step-definition body references a changed method or class name."
            copied.trace_path = sorted(changed_qnames) + [step_def.qualified_name, f"{item.feature_path}:{item.scenario_line}"]
            candidates.append(copied)
    return candidates


def impacted_step_definitions_from_candidates(
    step_defs: list[StepDefinition],
    candidates: list[ScenarioCandidate],
    symbols: list[SymbolInfo],
) -> list[StepDefinition]:
    impacted_qnames = {item.step_definition for item in candidates}
    changed_qnames = {symbol.qualified_name for symbol in symbols}
    changed_methods = {(symbol.file_path, symbol.name) for symbol in symbols}

    result = []
    for step_def in step_defs:
        if (
            step_def.qualified_name in impacted_qnames
            or step_def.qualified_name in changed_qnames
            or (step_def.file_path, step_def.method_name) in changed_methods
        ):
            result.append(step_def)
    return result


def discover_changes(repo: Path, base_ref: str, target_ref: str, include_all_source: bool = False) -> tuple[list[ChangedFile], str, str]:
    merge_base = run_git(repo, "merge-base", base_ref, target_ref).strip()
    target_sha = run_git(repo, "rev-parse", target_ref).strip()
    diff_output = run_git(repo, "diff", "--name-status", "--find-renames", merge_base, target_ref)
    changed_files: list[ChangedFile] = []

    for status, old_path, path in parse_name_status(diff_output):
        if should_ignore(path, include_all_source=include_all_source):
            continue
        diff = run_git(repo, "diff", "--unified=3", merge_base, target_ref, "--", path, check=False)
        ranges = split_changed_ranges(diff)
        content = read_target_file(repo, target_ref, path)
        old_content = read_ref_file(repo, merge_base, old_path or path)
        symbols, unmapped = map_ranges_to_symbols(content, old_content, path, ranges)
        changed_files.append(
            ChangedFile(
                status=status,
                path=path,
                old_path=old_path,
                language=language_for(path),
                diff=diff,
                structured_hunks=ranges,
                changed_symbols=symbols,
                unmapped_changes=unmapped,
            )
        )
    return changed_files, merge_base, target_sha


def build_trace_input(
    repo_path: Path,
    base_ref: str | None,
    target_ref: str,
    include_index: bool = False,
    include_all_source: bool = False,
) -> TraceInput:
    repo = validate_repo(repo_path)
    base = base_ref or default_base(repo)
    changed_files, base_sha, target_sha = discover_changes(repo, base, target_ref, include_all_source=include_all_source)
    step_defs = extract_step_definitions(repo)
    scenarios = parse_feature_files(repo)
    scenario_map = scenarios_for_step_definitions(step_defs, scenarios)
    changed_symbols = [symbol for file in changed_files for symbol in file.changed_symbols]
    candidates = dedupe_candidates(
        direct_step_definition_candidates(changed_symbols, step_defs, scenario_map)
        + simple_call_candidates(changed_symbols, step_defs, scenario_map, repo)
    )
    candidates = score_scenario_candidates(candidates, changed_symbols)
    impacted_step_defs = impacted_step_definitions_from_candidates(step_defs, candidates, changed_symbols)
    unresolved = [
        {
            "qualifiedName": symbol.qualified_name,
            "filePath": symbol.file_path,
            "reason": "No direct step-definition reference was found in the indexed slice.",
        }
        for symbol in changed_symbols
        if not any(symbol.name in " ".join(item.trace_path) for item in candidates)
    ]
    repository_index = None
    if include_index:
        repository_index = {
            "step_definitions": step_defs,
            "scenarios": scenarios,
        }

    return TraceInput(
        schema_version="trace-agent-input/v2",
        repo=str(repo),
        base_ref=base,
        base_sha=base_sha,
        target_ref=target_ref,
        target_sha=target_sha,
        changed_files=changed_files,
        impacted_step_definitions=impacted_step_defs,
        scenario_candidates=candidates,
        unresolved_symbols=unresolved,
        repository_index_summary={
            "total_step_definitions_indexed": len(step_defs),
            "total_scenarios_indexed": len(scenarios),
            "full_index_included": include_index,
        },
        repository_index=repository_index,
    )


def write_json(data: TraceInput, output: Path) -> None:
    def prune_none(value):
        if isinstance(value, dict):
            return {key: prune_none(item) for key, item in value.items() if item is not None}
        if isinstance(value, list):
            return [prune_none(item) for item in value]
        return value

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(prune_none(asdict(data)), indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build trace-agent input JSON from Git changes and Cucumber metadata.")
    parser.add_argument("--repo", default=".", help="Repository path.")
    parser.add_argument("--base", default=None, help="Base ref. Defaults to origin HEAD/master/main/develop.")
    parser.add_argument("--target", default="HEAD", help="Target ref to compare. Defaults to HEAD.")
    parser.add_argument("--output", default="runtime/trace-agent-input.json", help="Output JSON path.")
    parser.add_argument("--include-index", action="store_true", help="Include full step-definition and scenario indexes for debugging.")
    parser.add_argument(
        "--include-all-source",
        action="store_true",
        help="Analyze all supported source files, not only class files under src/main/java and src/test/java.",
    )
    args = parser.parse_args()

    trace_input = build_trace_input(
        Path(args.repo),
        args.base,
        args.target,
        include_index=args.include_index,
        include_all_source=args.include_all_source,
    )
    write_json(trace_input, Path(args.output))
    print(f"Wrote {args.output}")
    print(f"Changed files: {len(trace_input.changed_files)}")
    print(f"Changed symbols: {sum(len(item.changed_symbols) for item in trace_input.changed_files)}")
    print(f"Impacted step definitions: {len(trace_input.impacted_step_definitions)}")
    print(f"Scenario candidates: {len(trace_input.scenario_candidates)}")
    print(f"Unresolved symbols: {len(trace_input.unresolved_symbols)}")
    print(f"Full repository index included: {args.include_index}")
    print(f"Analyzed all source files: {args.include_all_source}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())