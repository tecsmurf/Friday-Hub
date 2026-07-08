"""Tests for friday.context — Project context gathering."""

import json
from pathlib import Path

import pytest

from friday.context.context_manager import ContextManager, ProjectContext, get_context


class TestProjectContext:
    """Test the ProjectContext dataclass."""

    def test_empty_context(self):
        """Empty context should produce a minimal prompt string."""
        ctx = ProjectContext()
        text = ctx.to_prompt_string()
        assert "Project:" in text
        assert "Directory:" in text

    def test_full_context_to_prompt(self):
        """Full context should include all fields in the prompt string."""
        ctx = ProjectContext(
            cwd="/home/user/project",
            project_name="my-app",
            project_types=["python", "node"],
            frameworks=["django", "react"],
            git_branch="main",
            git_status="3 modified",
            git_recent_commits=["abc1234 fix auth", "def5678 add tests"],
            file_tree=["src/", "  app.py", "tests/"],
            key_files=["pyproject.toml", "package.json"],
            total_files=42,
        )
        text = ctx.to_prompt_string()
        assert "my-app" in text
        assert "python, node" in text
        assert "django, react" in text
        assert "main" in text
        assert "3 modified" in text
        assert "fix auth" in text
        assert "42 files" in text
        assert "pyproject.toml" in text


class TestContextManager:
    """Test the ContextManager class."""

    def test_init_default_cwd(self):
        """Default cwd should be the current working directory."""
        mgr = ContextManager()
        assert mgr.cwd == Path.cwd()

    def test_init_custom_cwd(self, tmp_path):
        """Custom cwd should be used."""
        mgr = ContextManager(tmp_path)
        assert mgr.cwd == tmp_path

    def test_detect_python_project(self, tmp_path):
        """Should detect Python project from pyproject.toml."""
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'\n")
        mgr = ContextManager(tmp_path)
        types = mgr._detect_project_types()
        assert "python" in types

    def test_detect_node_project(self, tmp_path):
        """Should detect Node project from package.json."""
        (tmp_path / "package.json").write_text('{"name": "test"}')
        mgr = ContextManager(tmp_path)
        types = mgr._detect_project_types()
        assert "node" in types

    def test_detect_rust_project(self, tmp_path):
        """Should detect Rust project from Cargo.toml."""
        (tmp_path / "Cargo.toml").write_text("[package]\nname = 'test'\n")
        mgr = ContextManager(tmp_path)
        types = mgr._detect_project_types()
        assert "rust" in types

    def test_detect_go_project(self, tmp_path):
        """Should detect Go project from go.mod."""
        (tmp_path / "go.mod").write_text("module example.com/test\n")
        mgr = ContextManager(tmp_path)
        types = mgr._detect_project_types()
        assert "go" in types

    def test_detect_no_project(self, tmp_path):
        """Empty directory should have no detected project type."""
        mgr = ContextManager(tmp_path)
        types = mgr._detect_project_types()
        assert types == []

    def test_detect_multiple_types(self, tmp_path):
        """Should detect multiple project types."""
        (tmp_path / "pyproject.toml").write_text("[project]\n")
        (tmp_path / "package.json").write_text('{"name": "test"}')
        mgr = ContextManager(tmp_path)
        types = mgr._detect_project_types()
        assert "python" in types
        assert "node" in types

    def test_detect_key_files(self, tmp_path):
        """Should find key project files."""
        (tmp_path / "README.md").write_text("# Hello")
        (tmp_path / "Dockerfile").write_text("FROM python:3.12")
        (tmp_path / "Makefile").write_text("all:\n\techo hello")

        mgr = ContextManager(tmp_path)
        key = mgr._detect_key_files()
        assert "README.md" in key
        assert "Dockerfile" in key
        assert "Makefile" in key

    def test_build_file_tree(self, tmp_path):
        """Should build a file tree from the directory."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("print('hello')")
        (tmp_path / "README.md").write_text("# Test")

        mgr = ContextManager(tmp_path)
        tree, total = mgr._build_file_tree()
        assert total >= 2
        assert any("src/" in e for e in tree)

    def test_file_tree_skips_noise_dirs(self, tmp_path):
        """File tree should skip __pycache__, node_modules, etc."""
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "__pycache__" / "cache.pyc").write_text("")
        (tmp_path / "app.py").write_text("pass")

        mgr = ContextManager(tmp_path)
        tree, total = mgr._build_file_tree()
        assert not any("__pycache__" in e for e in tree)

    def test_detect_framework_from_package_json(self, tmp_path):
        """Should detect React from package.json dependencies."""
        pkg = {
            "name": "test-app",
            "dependencies": {"react": "^18.0.0", "react-dom": "^18.0.0"},
        }
        (tmp_path / "package.json").write_text(json.dumps(pkg))

        mgr = ContextManager(tmp_path)
        frameworks = mgr._detect_frameworks()
        assert "react" in frameworks

    def test_detect_framework_from_pyproject(self, tmp_path):
        """Should detect Django from pyproject.toml dependencies."""
        content = '[project]\ndependencies = ["django>=4.0"]\n'
        (tmp_path / "pyproject.toml").write_text(content)

        mgr = ContextManager(tmp_path)
        frameworks = mgr._detect_frameworks()
        assert "django" in frameworks

    def test_gather_full_context(self, tmp_path):
        """gather() should return a complete ProjectContext."""
        (tmp_path / "pyproject.toml").write_text("[project]\nname='test'\n")
        (tmp_path / "README.md").write_text("# Hello")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("pass")

        ctx = ContextManager(tmp_path).gather()
        assert isinstance(ctx, ProjectContext)
        assert ctx.project_name == tmp_path.name
        assert "python" in ctx.project_types
        assert "README.md" in ctx.key_files
        assert ctx.total_files >= 2

    def test_gather_non_git_dir(self, tmp_path):
        """gather() should handle non-git directories gracefully."""
        ctx = ContextManager(tmp_path).gather()
        assert ctx.git_branch is None
        assert ctx.git_status is None
        assert ctx.git_recent_commits == []


class TestGetContext:
    """Test the convenience function."""

    def test_get_context_returns_project_context(self, tmp_path):
        """get_context() should return a ProjectContext instance."""
        ctx = get_context(tmp_path)
        assert isinstance(ctx, ProjectContext)
        assert ctx.cwd == str(tmp_path)
