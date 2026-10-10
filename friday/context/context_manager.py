"""
Context Manager — Project-aware intelligence for agent prompts.

Scans the current working directory and collects:
  - Project type (Python, Node, Rust, Go, etc.)
  - Git status, branch, and recent commits
  - File tree summary (respecting gitignore patterns)
  - Detected frameworks and tools
  - Key configuration files

The resulting context string is injected into agent prompts so
they understand the project they're working on.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from dataclasses import dataclass, field


# ── Project type detection ────────────────────────────────────────────

_PROJECT_MARKERS: dict[str, list[str]] = {
    "python": ["pyproject.toml", "setup.py", "setup.cfg", "Pipfile", "requirements.txt"],
    "node": ["package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml"],
    "rust": ["Cargo.toml", "Cargo.lock"],
    "go": ["go.mod", "go.sum"],
    "java": ["pom.xml", "build.gradle", "build.gradle.kts"],
    "dotnet": ["*.csproj", "*.sln", "*.fsproj"],
    "ruby": ["Gemfile", "Rakefile"],
    "php": ["composer.json"],
}

_FRAMEWORK_MARKERS: dict[str, list[str]] = {
    "django": ["manage.py", "django"],
    "flask": ["flask"],
    "fastapi": ["fastapi"],
    "react": ["react"],
    "next.js": ["next.config.js", "next.config.mjs", "next.config.ts"],
    "vue": ["vue.config.js", "nuxt.config.ts"],
    "express": ["express"],
    "spring": ["spring"],
    "rails": ["config/routes.rb"],
}


@dataclass
class ProjectContext:
    """Structured representation of the working directory context."""

    cwd: str = ""
    project_name: str = ""
    project_types: list[str] = field(default_factory=list)
    frameworks: list[str] = field(default_factory=list)
    git_branch: str | None = None
    git_status: str | None = None
    git_recent_commits: list[str] = field(default_factory=list)
    file_tree: list[str] = field(default_factory=list)
    key_files: list[str] = field(default_factory=list)
    total_files: int = 0

    def to_prompt_string(self) -> str:
        """Format the context as a string suitable for injecting into prompts."""
        lines: list[str] = []
        lines.append(f"Project: {self.project_name}")
        lines.append(f"Directory: {self.cwd}")

        if self.project_types:
            lines.append(f"Type: {', '.join(self.project_types)}")
        if self.frameworks:
            lines.append(f"Frameworks: {', '.join(self.frameworks)}")

        if self.git_branch:
            lines.append(f"Git Branch: {self.git_branch}")
        if self.git_status:
            lines.append(f"Git Status: {self.git_status}")

        if self.git_recent_commits:
            lines.append("Recent Commits:")
            for commit in self.git_recent_commits[:5]:
                lines.append(f"  - {commit}")

        if self.file_tree:
            lines.append(f"File Tree ({self.total_files} files):")
            shown = self.file_tree[:30]
            for entry in shown:
                lines.append(f"  {entry}")
            if len(self.file_tree) > len(shown):
                lines.append("  ...")

        if self.key_files:
            lines.append("Key Files: " + ", ".join(self.key_files))

        return "\n".join(lines)


class ContextManager:
    """Collects project context from the working directory.

    Usage::

        ctx = ContextManager()
        context = ctx.gather()
        prompt_text = context.to_prompt_string()
    """

    def __init__(self, cwd: str | Path | None = None):
        self.cwd = Path(cwd) if cwd else Path.cwd()

    def gather(self) -> ProjectContext:
        """Gather all available project context."""
        context = ProjectContext(
            cwd=str(self.cwd),
            project_name=self.cwd.name,
        )

        context.project_types = self._detect_project_types()
        context.frameworks = self._detect_frameworks()
        context.key_files = self._detect_key_files()
        context.file_tree, context.total_files = self._build_file_tree()

        # Git info (may fail if not a git repo)
        context.git_branch = self._git_branch()
        context.git_status = self._git_status_summary()
        context.git_recent_commits = self._git_recent_commits()

        return context

    # ── Project type detection ──────────────────────────────────────

    def _detect_project_types(self) -> list[str]:
        """Detect project types based on marker files."""
        types: list[str] = []
        for project_type, markers in _PROJECT_MARKERS.items():
            for marker in markers:
                if "*" in marker:
                    # Glob pattern
                    if list(self.cwd.glob(marker)):
                        types.append(project_type)
                        break
                elif (self.cwd / marker).exists():
                    types.append(project_type)
                    break
        return types

    def _detect_frameworks(self) -> list[str]:
        """Detect frameworks from config files and dependencies."""
        frameworks: list[str] = []

        # Check file-based markers
        for framework, markers in _FRAMEWORK_MARKERS.items():
            for marker in markers:
                if "/" in marker:
                    # Path marker
                    if (self.cwd / marker).exists():
                        frameworks.append(framework)
                        break
                elif (self.cwd / marker).exists():
                    frameworks.append(framework)
                    break

        # Check package.json dependencies
        pkg_json = self.cwd / "package.json"
        if pkg_json.exists():
            try:
                import json

                data = json.loads(pkg_json.read_text(encoding="utf-8"))
                all_deps = {
                    **data.get("dependencies", {}),
                    **data.get("devDependencies", {}),
                }
                for framework, markers in _FRAMEWORK_MARKERS.items():
                    if framework not in frameworks:
                        for marker in markers:
                            if marker in all_deps:
                                frameworks.append(framework)
                                break
            except Exception:
                pass

        # Check pyproject.toml / requirements.txt dependencies
        for dep_file in ("pyproject.toml", "requirements.txt"):
            path = self.cwd / dep_file
            if path.exists():
                try:
                    content = path.read_text(encoding="utf-8").lower()
                    for framework, markers in _FRAMEWORK_MARKERS.items():
                        if framework not in frameworks:
                            for marker in markers:
                                if marker.lower() in content:
                                    frameworks.append(framework)
                                    break
                except Exception:
                    pass

        return frameworks

    # ── Key files ───────────────────────────────────────────────────

    def _detect_key_files(self) -> list[str]:
        """Find important project files."""
        candidates = [
            "README.md", "README.rst", "README.txt",
            "pyproject.toml", "setup.py", "setup.cfg",
            "package.json", "Cargo.toml", "go.mod",
            "Makefile", "Dockerfile", "docker-compose.yml",
            ".github/workflows", ".gitlab-ci.yml",
            "Jenkinsfile", "Procfile",
        ]
        found: list[str] = []
        for name in candidates:
            if (self.cwd / name).exists():
                found.append(name)
        return found

    # ── File tree ───────────────────────────────────────────────────

    def _build_file_tree(
        self, max_depth: int = 3, max_entries: int = 50, max_scanned: int = 5000
    ) -> tuple[list[str], int]:
        """Build a compact file tree, skipping hidden and common noise directories.

        The walk is bounded (``max_scanned`` filesystem entries) so running
        Friday from a large directory such as ``$HOME`` stays fast. When the
        bound is hit, ``total`` is a lower bound.
        """
        skip_dirs = {
            "__pycache__", "node_modules", "venv", "dist", "build",
            "target", "bin", "obj", "site-packages",
        }
        entries: list[str] = []
        total = 0
        scanned = 0

        def _skip(path: Path) -> bool:
            name = path.name
            return name.startswith(".") or name in skip_dirs or name.endswith(".egg-info")

        def _walk(path: Path, prefix: str, depth: int) -> None:
            nonlocal total, scanned
            if depth > max_depth or scanned >= max_scanned:
                return

            try:
                children = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
            except OSError:
                return
            scanned += len(children)

            dirs = [c for c in children if c.is_dir() and not c.is_symlink() and not _skip(c)]
            files = [c for c in children if c.is_file() and not c.name.startswith(".")]

            for d in dirs:
                if len(entries) < max_entries:
                    entries.append(f"{prefix}{d.name}/")
                _walk(d, prefix + "  ", depth + 1)

            for f in files:
                total += 1
                if len(entries) < max_entries:
                    entries.append(f"{prefix}{f.name}")

        _walk(self.cwd, "", 0)
        return entries, total

    # ── Git helpers ─────────────────────────────────────────────────

    def _run_git(self, *args: str) -> str | None:
        """Run a git command and return stdout, or None on failure."""
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=str(self.cwd),
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return None

    def _git_branch(self) -> str | None:
        """Get the current git branch name."""
        return self._run_git("branch", "--show-current")

    def _git_status_summary(self) -> str | None:
        """Get a one-line git status summary."""
        status = self._run_git("status", "--porcelain")
        if status is None:
            return None
        lines = status.strip().split("\n") if status.strip() else []
        if not lines:
            return "clean"
        codes = [line[:2] for line in lines]
        added = sum(1 for c in codes if c == "??" or "A" in c)
        deleted = sum(1 for c in codes if "D" in c and "A" not in c)
        modified = sum(1 for c in codes if c != "??" and "A" not in c and "D" not in c)
        parts: list[str] = []
        if modified:
            parts.append(f"{modified} modified")
        if added:
            parts.append(f"{added} added/untracked")
        if deleted:
            parts.append(f"{deleted} deleted")
        return ", ".join(parts) if parts else f"{len(lines)} changes"

    def _git_recent_commits(self, n: int = 5) -> list[str]:
        """Get recent commit one-liners."""
        output = self._run_git("log", f"-{n}", "--oneline", "--no-decorate")
        if output:
            return output.strip().split("\n")
        return []


def get_context(cwd: str | Path | None = None) -> ProjectContext:
    """Convenience function — gather project context for the given directory.

    Args:
        cwd: Working directory to scan. Defaults to ``os.getcwd()``.

    Returns:
        A ``ProjectContext`` instance with all detected metadata.
    """
    return ContextManager(cwd).gather()
