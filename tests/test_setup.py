"""setup: idempotent, reversible, backed up, and never leaves a broken config."""
import json
import os
from pathlib import Path
import tempfile
import tomllib
import unittest

import support  # noqa: F401
import setup_surfaces as setup


class Home(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(prefix="kn-")
        self.addCleanup(self.dir.cleanup)
        self.home = Path(self.dir.name)
        self.saved = setup.HOME, setup.BIN
        setup.HOME, setup.BIN = self.home, self.home / ".local/bin/kilix-needle"
        self.addCleanup(lambda: (setattr(setup, "HOME", self.saved[0]),
                                 setattr(setup, "BIN", self.saved[1])))
        os.environ["KILIX_CONFIG_HOME"] = str(self.home / "kilixconf")
        self.addCleanup(os.environ.pop, "KILIX_CONFIG_HOME", None)

    def run_setup(self, names, **kw):
        status, lines = setup.setup(names, **kw)
        return status, "\n".join(lines)

    def test_a_block_followed_by_user_lines_stays_where_it_is(self):
        # Measured: rerunning setup moved the block past a later user line and
        # reported "updated" for an identical file.
        aliases = self.home / ".bash_aliases"
        self.run_setup(["alias"])
        aliases.write_text(aliases.read_text() + "watch(){ true; }\n")
        before = aliases.read_text()
        status, out = self.run_setup(["alias"])
        self.assertEqual(status, 0)
        self.assertEqual(aliases.read_text(), before)
        self.assertIn("unchanged", out)

    def test_a_symlinked_dotfile_stays_a_symlink_and_its_target_is_edited(self):
        managed = self.home / "dotfiles/bash_aliases"
        managed.parent.mkdir()
        managed.write_text("alias ll='ls -l'\n")
        link = self.home / ".bash_aliases"
        link.symlink_to(managed)
        self.run_setup(["alias"])
        self.assertTrue(link.is_symlink())
        self.assertIn("alias kn=", managed.read_text())
        self.run_setup(["alias"], undo=True)
        self.assertTrue(link.is_symlink())
        self.assertEqual(managed.read_text(), "alias ll='ls -l'\n")

    def test_text_surfaces_are_idempotent_and_reversible(self):
        aliases = self.home / ".bash_aliases"
        aliases.write_text("alias ll='ls -l'\n")
        conf = self.home / "kilixconf/kitty.conf"
        conf.parent.mkdir()
        conf.write_text("include .kilix-defaults.conf\n")
        for _ in range(2):
            status, _ = self.run_setup(["alias", "hotkey"])
            self.assertEqual(status, 0)
        self.assertEqual(aliases.read_text().count("alias kn="), 1)
        self.assertIn("map ctrl+alt+space launch --type=overlay", conf.read_text())
        self.assertTrue((self.home / ".bash_aliases.kilix-needle.bak").exists())
        self.run_setup(["alias", "hotkey"], undo=True)
        self.assertEqual(aliases.read_text(), "alias ll='ls -l'\n")
        self.assertEqual(conf.read_text(), "include .kilix-defaults.conf\n")

    def test_dry_run_changes_nothing(self):
        status, out = self.run_setup(["alias", "codex", "command", "omp"], dry_run=True)
        self.assertEqual(status, 0)
        self.assertIn("would", out)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_codex_block_parses_and_keeps_existing_tables(self):
        config = self.home / ".codex/config.toml"
        config.parent.mkdir()
        config.write_text('model = "x"\n\n[mcp_servers.other]\ncommand = "o"\n')
        self.run_setup(["codex"])
        parsed = tomllib.loads(config.read_text())
        self.assertEqual(parsed["model"], "x")
        self.assertEqual(parsed["mcp_servers"]["kilix-needle"]["args"], ["mcp"])
        self.assertIn("KITTY_WINDOW_ID", parsed["mcp_servers"]["kilix-needle"]["env_vars"])
        self.assertIn("other", parsed["mcp_servers"])

    def test_an_edit_that_would_not_parse_leaves_the_file_alone(self):
        config = self.home / ".codex/config.toml"
        config.parent.mkdir()
        # an existing table of the same name makes our block a duplicate table
        original = '[mcp_servers.kilix-needle]\ncommand = "mine"\n'
        config.write_text(original)
        status, out = self.run_setup(["codex"])
        self.assertEqual(status, 1)
        self.assertIn("would not parse", out)
        self.assertEqual(config.read_text(), original)

    def test_omp_json_merge_and_undo(self):
        agent = self.home / ".omp/agent"
        agent.mkdir(parents=True)
        (agent / "mcp.json").write_text(json.dumps({"mcpServers": {"x": {"command": "x"}}}))
        self.run_setup(["omp"])
        data = json.loads((agent / "mcp.json").read_text())
        self.assertEqual(data["mcpServers"]["kilix-needle"]["args"], ["mcp"])
        self.assertIn("x", data["mcpServers"])
        self.run_setup(["omp"], undo=True)
        self.assertEqual(json.loads((agent / "mcp.json").read_text()),
                         {"mcpServers": {"x": {"command": "x"}}})

    def test_command_link_never_replaces_a_foreign_file(self):
        setup.BIN.parent.mkdir(parents=True)
        setup.BIN.write_text("someone else's")
        status, out = self.run_setup(["command"])
        self.assertEqual(status, 1)
        self.assertEqual(setup.BIN.read_text(), "someone else's")
        setup.BIN.unlink()
        self.run_setup(["command"])
        self.assertEqual(Path(os.readlink(setup.BIN)), setup.SCRIPT)
        self.run_setup(["command"], undo=True)
        self.assertFalse(setup.BIN.exists())


if __name__ == "__main__":
    unittest.main()


class SymlinkedConfigs(unittest.TestCase):
    """R2 KN-R2-05, KN-R2-11 and mutant R20."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(prefix="kn-")
        self.addCleanup(self.dir.cleanup)
        self.home = Path(self.dir.name)
        saved = setup.HOME, setup.BIN
        setup.HOME, setup.BIN = self.home, self.home / ".local/bin/kilix-needle"
        self.addCleanup(lambda: (setattr(setup, "HOME", saved[0]), setattr(setup, "BIN", saved[1])))
        (self.home / "dotfiles").mkdir()

    def test_a_relative_link_is_followed_and_the_backup_stays_out_of_the_repo(self):
        (self.home / "dotfiles/bash_aliases").write_text("alias ll='ls -l'\n")
        link = self.home / ".bash_aliases"
        link.symlink_to("dotfiles/bash_aliases")          # relative, as stow makes them
        cwd = os.getcwd()
        os.chdir("/")                                       # a relative link must not resolve here
        self.addCleanup(os.chdir, cwd)
        setup.setup(["alias"])
        self.assertTrue(link.is_symlink())
        self.assertIn("alias kn=", (self.home / "dotfiles/bash_aliases").read_text())
        self.assertEqual(sorted(p.name for p in (self.home / "dotfiles").iterdir()), ["bash_aliases"])

    def test_the_omp_config_link_is_kept(self):
        (self.home / "dotfiles/mcp.json").write_text('{"mcpServers": {}}\n')
        (self.home / ".omp/agent").mkdir(parents=True)
        link = self.home / ".omp/agent/mcp.json"
        link.symlink_to(self.home / "dotfiles/mcp.json")
        setup.setup(["omp"])
        self.assertTrue(link.is_symlink())
        self.assertIn("kilix-needle", json.loads((self.home / "dotfiles/mcp.json").read_text())["mcpServers"])
        setup.setup(["omp"], undo=True)
        self.assertTrue(link.is_symlink())


class ConfigFileDetails(SymlinkedConfigs):
    def test_a_config_keeps_its_mode(self):                     # R3 mutant S04
        aliases = self.home / ".bash_aliases"
        aliases.write_text("alias ll='ls -l'\n")
        os.chmod(aliases, 0o644)
        setup.setup(["alias"])
        self.assertEqual(aliases.stat().st_mode & 0o777, 0o644)

    def test_omp_undo_restores_the_users_formatting(self):      # KN-R3-09
        (self.home / ".omp/agent").mkdir(parents=True)
        mcp = self.home / ".omp/agent/mcp.json"
        original = '{"mcpServers": {}}\n'
        mcp.write_text(original)
        setup.setup(["omp"])
        setup.setup(["omp"], undo=True)
        self.assertEqual(mcp.read_text(), original)


class OmpUndoResidue(SymlinkedConfigs):
    """Review KN-R4-10 and its mutant M19."""

    def omp(self):
        (self.home / ".omp/agent").mkdir(parents=True, exist_ok=True)
        return self.home / ".omp/agent/mcp.json"

    def test_an_empty_object_comes_back_as_it_was(self):
        mcp = self.omp()
        mcp.write_text("{}\n")
        setup.setup(["omp"]); setup.setup(["omp"], undo=True)
        self.assertEqual(mcp.read_text(), "{}\n")

    def test_a_file_setup_created_is_removed(self):
        mcp = self.omp()
        setup.setup(["omp"]); setup.setup(["omp"], undo=True)
        self.assertFalse(mcp.exists())

    def test_a_later_user_edit_survives_undo(self):                    # M19
        mcp = self.omp()
        mcp.write_text('{"mcpServers": {}}\n')
        setup.setup(["omp"])
        data = json.loads(mcp.read_text()); data["mcpServers"]["mine"] = {"command": "x"}
        mcp.write_text(json.dumps(data))
        setup.setup(["omp"], undo=True)
        self.assertIn("mine", json.loads(mcp.read_text())["mcpServers"])


class OmpCreationMarker(OmpUndoResidue):
    """KN-R5-08, survivors O02 and O04."""

    def test_a_users_own_file_is_never_removed_without_the_marker(self):
        mcp = self.omp()
        mcp.write_text("{}\n")
        setup.setup(["omp"])
        (mcp.parent / "mcp.json.kilix-needle.bak").unlink()
        setup.setup(["omp"], undo=True)
        self.assertTrue(mcp.exists())

    def test_a_created_file_with_a_later_user_edit_survives(self):   # O02
        mcp = self.omp()
        setup.setup(["omp"])
        data = json.loads(mcp.read_text()); data["mcpServers"]["mine"] = {"command": "x"}
        mcp.write_text(json.dumps(data))
        setup.setup(["omp"], undo=True)
        self.assertIn("mine", json.loads(mcp.read_text())["mcpServers"])

    def test_a_created_target_is_removed_and_the_link_kept(self):    # O04
        target = self.home / "dotfiles/mcp.json"
        (self.home / ".omp/agent").mkdir(parents=True)
        link = self.home / ".omp/agent/mcp.json"
        link.symlink_to(target)                                   # dangling until setup
        setup.setup(["omp"])
        self.assertTrue(target.exists())
        setup.setup(["omp"], undo=True)
        self.assertFalse(target.exists())
        self.assertTrue(link.is_symlink())


class OmpMarkerNeverStale(OmpUndoResidue):
    def test_undo_always_removes_the_marker(self):                        # O02
        mcp = self.omp()
        setup.setup(["omp"])
        marker = mcp.parent / "mcp.json.kilix-needle.created"
        self.assertTrue(marker.exists())
        data = json.loads(mcp.read_text()); data["mcpServers"]["mine"] = {"command": "x"}
        mcp.write_text(json.dumps(data))
        setup.setup(["omp"], undo=True)
        self.assertFalse(marker.exists())
