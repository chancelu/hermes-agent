"""Skill ``depends_on`` declaration and install-time enforcement (#71853).

Skills already carry ``prerequisites`` (env vars, commands) and
``related_skills`` (advisory cross-references), but neither says "this skill
does not work without that one", and nothing was enforced at install time. A
skill that drives another skill's commands installed cleanly on its own and
only failed once the agent reached for the missing piece.
"""
from types import SimpleNamespace

import pytest

from tools.skills_hub import (
    SkillDependency,
    parse_skill_dependencies,
    resolve_skill_dependencies,
)


class TestParsing:
    def test_bare_list_means_all_required(self):
        deps = parse_skill_dependencies({"depends_on": ["obsidian", "kanban"]})
        assert [(d.name, d.required) for d in deps] == [("obsidian", True), ("kanban", True)]

    def test_mapping_form_carries_required_and_reason(self):
        deps = parse_skill_dependencies({
            "depends_on": [
                {"name": "obsidian", "required": True, "reason": "reads from vaults"},
                {"name": "kanban", "required": False, "reason": "embeds board status"},
            ]
        })
        assert deps[0] == SkillDependency("obsidian", True, "reads from vaults")
        assert deps[1] == SkillDependency("kanban", False, "embeds board status")

    def test_absent_or_empty_is_no_dependencies(self):
        assert parse_skill_dependencies({}) == []
        assert parse_skill_dependencies({"depends_on": []}) == []
        assert parse_skill_dependencies({"depends_on": None}) == []

    def test_single_string_is_accepted(self):
        assert [d.name for d in parse_skill_dependencies({"depends_on": "obsidian"})] == ["obsidian"]

    def test_duplicates_collapse(self):
        deps = parse_skill_dependencies({"depends_on": ["a", "a", {"name": "a"}]})
        assert len(deps) == 1

    def test_malformed_entries_are_skipped_not_raised(self):
        """A bad depends_on must not make an installable skill uninstallable."""
        deps = parse_skill_dependencies({
            "depends_on": [123, None, {"required": True}, {"name": "  "}, "good"]
        })
        assert [d.name for d in deps] == ["good"]

    def test_non_list_scalar_is_ignored(self):
        assert parse_skill_dependencies({"depends_on": {"name": "x"}}) == []


class TestResolution:
    DEPS = [
        SkillDependency("obsidian", True, "vaults"),
        SkillDependency("kanban", False, "board status"),
    ]

    def test_splits_missing_required_from_missing_optional(self):
        req, opt = resolve_skill_dependencies(self.DEPS, installed=set())
        assert [d.name for d in req] == ["obsidian"]
        assert [d.name for d in opt] == ["kanban"]

    def test_nothing_missing_when_all_present(self):
        req, opt = resolve_skill_dependencies(self.DEPS, installed={"obsidian", "kanban"})
        assert req == [] and opt == []

    def test_optional_present_required_missing(self):
        req, opt = resolve_skill_dependencies(self.DEPS, installed={"kanban"})
        assert [d.name for d in req] == ["obsidian"]
        assert opt == []


class TestInstalledDiscovery:
    """The gate must agree with what the agent can actually load.

    Anything runtime discovery can resolve is "installed" for this purpose;
    reporting it missing blocks a dependency the user genuinely has.
    """

    def _roots(self, monkeypatch, *roots):
        import tools.skills_hub as hub

        monkeypatch.setattr(hub.HubLockFile, "list_installed", lambda self: [])
        monkeypatch.setattr(
            "agent.skill_utils.get_all_skills_dirs", lambda: list(roots)
        )
        return hub

    def _skill(self, root, rel, *, declared=None):
        d = root / rel
        d.mkdir(parents=True, exist_ok=True)
        name_line = f"name: {declared}\n" if declared else ""
        (d / "SKILL.md").write_text(f"---\n{name_line}description: x\n---\n")
        return d

    def test_local_skills_count_even_without_a_lockfile_entry(self, tmp_path, monkeypatch):
        local = tmp_path / "skills"
        self._skill(local, "obsidian")
        self._skill(local, "productivity/kanban")
        self._skill(local, ".trash/ghost")          # hub internals are not skills

        hub = self._roots(monkeypatch, local)
        names = hub.installed_skill_names()

        assert {"obsidian", "kanban"} <= names, (
            "an official/hand-placed skill was not counted as installed, so its "
            "dependents would be blocked for a dependency the user has"
        )
        assert "ghost" not in names

    def test_external_roots_are_scanned(self, tmp_path, monkeypatch):
        """skills.external_dirs are active skill roots at runtime.

        get_all_skills_dirs() returns the local dir plus every configured
        external dir; a dependency supplied from one of those is as usable as
        one in the profile, so scanning only the profile falsely blocks it.
        """
        local = tmp_path / "skills"
        external = tmp_path / "team-skills"
        self._skill(local, "mine")
        self._skill(external, "obsidian")

        hub = self._roots(monkeypatch, local, external)
        names = hub.installed_skill_names()

        assert "obsidian" in names, (
            "a skill provided by an external root was reported missing — its "
            "dependents would be blocked despite being loadable at runtime"
        )
        assert "mine" in names

    def test_declared_name_and_directory_name_both_resolve(self, tmp_path, monkeypatch):
        """Runtime resolves ``frontmatter.get("name", skill_dir.name)``.

        A hand-placed skill in a differently-named directory is usable under
        its declared name, so both spellings have to satisfy a dependency.
        """
        local = tmp_path / "skills"
        self._skill(local, "vendor-obsidian-v2", declared="obsidian")

        hub = self._roots(monkeypatch, local)
        names = hub.installed_skill_names()

        assert "obsidian" in names, (
            "depends_on: [obsidian] would be blocked even though the agent can "
            "load that skill by its declared name"
        )
        assert "vendor-obsidian-v2" in names  # the directory spelling still works

    def test_unreadable_frontmatter_falls_back_to_the_directory_name(self, tmp_path, monkeypatch):
        local = tmp_path / "skills"
        d = local / "kanban"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("no frontmatter at all\n")

        hub = self._roots(monkeypatch, local)
        assert "kanban" in hub.installed_skill_names()

    def test_a_missing_root_does_not_break_the_others(self, tmp_path, monkeypatch):
        local = tmp_path / "skills"
        self._skill(local, "obsidian")
        hub = self._roots(monkeypatch, local, tmp_path / "does-not-exist")
        assert "obsidian" in hub.installed_skill_names()


def _bundle(depends_on_yaml: str, name: str = "chronicle"):
    md = f"---\nname: {name}\ndescription: test\n{depends_on_yaml}---\n\n# body\n"
    return SimpleNamespace(name=name, files={"SKILL.md": md})


class TestInstallEnforcement:
    """The gate itself: required blocks, optional warns, --force overrides."""

    @pytest.fixture()
    def console(self):
        class _C:
            def __init__(self):
                self.out = []

            def print(self, *a, **k):
                self.out.append(" ".join(str(x) for x in a))

            @property
            def text(self):
                return "\n".join(self.out)
        return _C()

    def _check(self, monkeypatch, bundle, console, *, installed=frozenset(), force=False):
        import tools.skills_hub as hub
        from hermes_cli.skills_hub import _check_skill_dependencies

        monkeypatch.setattr(hub, "installed_skill_names", lambda: set(installed))
        return _check_skill_dependencies(bundle, console, force=force)

    def test_missing_required_blocks_and_names_the_fix(self, monkeypatch, console):
        b = _bundle("depends_on:\n  - name: obsidian\n    reason: reads vaults\n")
        blocked = self._check(monkeypatch, b, console)

        assert blocked is True, (
            "install proceeded without a required dependency — the failure "
            "would only surface when the agent used the skill"
        )
        assert "obsidian" in console.text
        assert "hermes skill install obsidian" in console.text
        assert "reads vaults" in console.text

    def test_missing_optional_warns_but_proceeds(self, monkeypatch, console):
        b = _bundle("depends_on:\n  - name: kanban\n    required: false\n")
        assert self._check(monkeypatch, b, console) is False
        assert "kanban" in console.text
        assert "reduced functionality" in console.text

    def test_satisfied_dependencies_are_silent(self, monkeypatch, console):
        b = _bundle("depends_on: [obsidian]\n")
        assert self._check(monkeypatch, b, console, installed={"obsidian"}) is False
        assert console.text == ""

    def test_force_downgrades_the_block_to_a_warning(self, monkeypatch, console):
        b = _bundle("depends_on: [obsidian]\n")
        assert self._check(monkeypatch, b, console, force=True) is False
        assert "--force" in console.text or "force" in console.text.lower()

    def test_skill_without_depends_on_is_untouched(self, monkeypatch, console):
        b = _bundle("")
        assert self._check(monkeypatch, b, console) is False
        assert console.text == ""

    def test_bundle_without_a_skill_md_is_untouched(self, monkeypatch, console):
        b = SimpleNamespace(name="x", files={})
        assert self._check(monkeypatch, b, console) is False

    def test_broken_frontmatter_does_not_block(self, monkeypatch, console):
        """Unparseable YAML must not make a skill uninstallable."""
        b = SimpleNamespace(name="x", files={"SKILL.md": "---\nname: [unclosed\n---\n"})
        assert self._check(monkeypatch, b, console) is False

    def test_required_and_optional_together(self, monkeypatch, console):
        b = _bundle(
            "depends_on:\n"
            "  - name: obsidian\n"
            "    required: true\n"
            "  - name: kanban\n"
            "    required: false\n"
        )
        assert self._check(monkeypatch, b, console) is True
        assert "obsidian" in console.text and "kanban" in console.text


class TestDoInstallActuallyCallsTheGate:
    """The gate must be wired into do_install, not merely defined.

    Every test above calls ``_check_skill_dependencies`` directly, so all of
    them pass with the call site deleted from ``do_install`` — the feature
    would ship declared and unenforced, which is the exact thing this issue is
    about. These drive the real install path instead.
    """

    @pytest.fixture()
    def served(self, tmp_path, monkeypatch):
        """A minimal one-file skill served over loopback, per test_skill_bundle_provenance."""
        import subprocess
        from http.server import SimpleHTTPRequestHandler
        import socketserver
        import threading

        monkeypatch.setattr("tools.url_safety._global_allow_private_urls", lambda: True)

        def _make(depends_on_yaml: str):
            repo = tmp_path / f"upstream{len(depends_on_yaml)}"
            repo.mkdir()
            (repo / "SKILL.md").write_text(
                f"---\nname: needs-dep\ndescription: A test skill.\n{depends_on_yaml}---\n\n# Body\n"
            )
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(
                ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"],
                cwd=repo, check=True,
            )

            class _Quiet(SimpleHTTPRequestHandler):
                def log_message(self, *_a):
                    pass

                def translate_path(self, path):
                    import os
                    rel = path.split("?", 1)[0].lstrip("/")
                    return os.path.join(str(repo), rel)

            httpd = socketserver.TCPServer(("127.0.0.1", 0), _Quiet)
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            url = f"http://127.0.0.1:{httpd.server_address[1]}/SKILL.md"
            return url, httpd

        return _make

    def _run(self, monkeypatch, tmp_path, served, depends_on_yaml, *, installed=frozenset()):
        from io import StringIO

        from rich.console import Console

        import tools.skills_hub as hub
        from hermes_cli.skills_hub import do_install
        from tools.skills_hub import UrlSource

        url, httpd = served(depends_on_yaml)
        home = tmp_path / "home"
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setattr("tools.skills_hub.is_safe_url", lambda _u: True)
        monkeypatch.setattr("tools.skills_hub.check_website_access", lambda _u: None)
        monkeypatch.setattr(hub, "create_source_router", lambda auth=None: [UrlSource()])
        monkeypatch.setattr(hub, "installed_skill_names", lambda: set(installed))

        sink = StringIO()
        try:
            do_install(url, console=Console(file=sink, force_terminal=False),
                       skip_confirm=True, name_override="needs-dep")
        finally:
            httpd.shutdown()
        return home, sink.getvalue()

    def test_unsatisfied_required_dependency_stops_the_install(
        self, monkeypatch, tmp_path, served
    ):
        home, out = self._run(
            monkeypatch, tmp_path, served,
            "depends_on:\n  - name: obsidian\n    reason: reads vaults\n",
        )
        assert "Installation blocked" in out, (
            "do_install ran to completion with a missing required dependency — "
            "the gate is defined but never called"
        )
        assert not (home / "skills" / "needs-dep").exists(), (
            "the skill landed on disk despite the block"
        )

    def test_satisfied_dependency_installs_normally(self, monkeypatch, tmp_path, served):
        home, out = self._run(
            monkeypatch, tmp_path, served,
            "depends_on: [obsidian]\n",
            installed={"obsidian"},
        )
        assert "Installation blocked" not in out, (
            "a skill whose dependency IS installed was blocked anyway"
        )
        assert "Installed: needs-dep" in out, (
            f"the install did not complete:\n{out}"
        )
