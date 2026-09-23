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
