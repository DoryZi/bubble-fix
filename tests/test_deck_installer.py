"""The plan commit, the registry rules, and the installer's links and blocks."""
from __future__ import annotations

from pathlib import Path

import pytest

from lib import deck, installer, paths, registry
from lib.registry import LogSource, Project


def test_a_plan_commit_that_fails_says_why(project: Project) -> None:
    """Code review before publish: a refused commit is reported with git's reason, not as plain "saved"."""
    import subprocess

    git = ["git", "-C", str(project.root), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(project.root)], check=True)
    (project.root / ".gitignore").write_text(".bubble-fix/\ncycles/*/.work/\n")
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "start"], check=True)
    hook = project.root / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'blocked by a hook' >&2\nexit 1\n")
    hook.chmod(0o755)
    base = project.root / "cycles" / "2026-11-01-01-plan-cycle-1"
    base.mkdir(parents=True)
    (base / "plan.md").write_text("# plan\n")
    line = deck._commit(registry.get("app"), base, ["plan.md"], "bubble-fix plan cycle 1: plan")
    assert "not committed: blocked by a hook" in line


def test_origins_belong_to_one_project_only(project: Project, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="already belongs"):
        registry.register("other", tmp_path / "other", ["http://LOCALHOST:5199/"], [])
    assert registry.for_origin("http://localhost:5199").name == "app"
    assert registry.for_origin("http://localhost:5200") is None
    assert registry.for_path(project.root / "sub" / "dir").name == "app"


def test_bad_log_format_and_origin_are_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="log format"):
        registry.register("x", tmp_path, ["http://localhost:1"], [LogSource("a", "xml")])
    with pytest.raises(ValueError, match="not an app address"):
        registry.register("x", tmp_path, ["localhost:1"], [])


def test_link_never_replaces_a_real_folder(tmp_path: Path) -> None:
    target = tmp_path / "skill"
    target.mkdir()
    real = tmp_path / "skills" / "bubble-fix"
    real.mkdir(parents=True)
    assert installer.link(target, real).startswith("SKIPPED")
    link = tmp_path / "links" / "bubble-fix"
    assert installer.link(target, link).startswith("linked")
    assert installer.link(target, link).startswith("ok")


def test_rules_block_and_gitignore_are_idempotent(tmp_path: Path) -> None:
    doc = tmp_path / "CLAUDE.md"
    doc.write_text("# Project\n\nRules.\n")
    assert installer.upsert_rules(doc).startswith("updated")
    once = doc.read_text()
    assert installer.upsert_rules(doc).startswith("ok") and doc.read_text() == once
    assert once.startswith("# Project") and once.count(installer.START) == 1
    assert installer.gitignore(tmp_path).startswith("updated")
    assert installer.gitignore(tmp_path).startswith("ok")
    assert ".bubble-fix/" in (tmp_path / ".gitignore").read_text()


def test_unit_file_runs_the_collector_with_uv(tmp_path: Path) -> None:
    text = installer.unit_text()
    assert "bubble_fix.py serve" in text and "run --quiet --directory" in text and "{" not in text


def test_upsert_rules_replaces_an_old_fix_heal_loop_block(tmp_path: Path) -> None:
    doc = tmp_path / "AGENTS.md"
    doc.write_text("# Project\n\n<!-- fix-heal-loop:start -->\n## Fix-heal-loop\nold rules\n<!-- fix-heal-loop:end -->\n\nAfter.\n")
    assert installer.upsert_rules(doc).startswith("updated")
    text = doc.read_text()
    assert "fix-heal-loop" not in text and "old rules" not in text
    assert text.count(installer.START) == 1 and text.startswith("# Project") and text.rstrip().endswith("After.")
    assert installer.upsert_rules(doc).startswith("ok")


ALIAS = paths.ALIAS_SKILL_DIR.is_dir()  # the public copy has no old-name alias skill
needs_alias = pytest.mark.skipif(not ALIAS, reason="no alias-skill folder")


def test_install_dry_run_lists_the_service_and_four_links(tmp_path: Path, monkeypatch) -> None:
    claude, agents = tmp_path / ".claude" / "skills", tmp_path / ".agents" / "skills"
    monkeypatch.setattr(installer, "SKILL_LINKS", (claude / "bubble-fix", agents / "bubble-fix"))
    monkeypatch.setattr(installer, "ALIAS_LINKS", (claude / "fix-heal-loop", agents / "fix-heal-loop"))
    monkeypatch.setattr(installer, "UNIT_PATH", tmp_path / "bubble-fix.service")
    lines = installer.install_user(dry_run=True)
    assert len([line for line in lines if line.startswith("linked")]) == (4 if ALIAS else 2)
    assert f"{claude / 'bubble-fix'} -> {paths.SKILL_DIR}" in "\n".join(lines)
    assert "enable --now bubble-fix" in lines[-1] and not (claude / "bubble-fix").exists()
    if ALIAS:
        assert f"{agents / 'fix-heal-loop'} -> {paths.ALIAS_SKILL_DIR}" in "\n".join(lines)
        assert (paths.ALIAS_SKILL_DIR / "SKILL.md").read_text().startswith("---\nname: fix-heal-loop")


@needs_alias
def test_alias_link_replaces_an_old_link(tmp_path: Path) -> None:
    old_target, link = tmp_path / "old-skill", tmp_path / "skills" / "fix-heal-loop"
    old_target.mkdir()
    link.parent.mkdir()
    link.symlink_to(old_target)
    assert installer.link(paths.ALIAS_SKILL_DIR, link).startswith("linked") and link.resolve() == paths.ALIAS_SKILL_DIR.resolve()
