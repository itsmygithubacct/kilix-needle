"""Make kilix-needle reachable: a command, an alias, a Kilix hotkey, agent tools.

    kilix-needle setup [--dry-run] [--undo] [--only NAME,...]

Surfaces (NAME):
  command  ~/.local/bin/kilix-needle -> this checkout
  alias    `kn` in ~/.bash_aliases
  hotkey   ctrl+alt+space in the Kilix user kitty.conf: kilix-needle as an
           overlay acting on the pane beneath it
  claude   `claude mcp add --scope user kilix-needle`
  codex    [mcp_servers.kilix-needle] in ~/.codex/config.toml, passing the
           KITTY_* variables through
  grok     `grok mcp add --scope user kilix-needle`
  omp      mcpServers.kilix-needle in ~/.omp/agent/mcp.json (the host omp;
           omp.sh's Docker sandbox cannot reach Kilix's abstract socket)

Every change is idempotent and reversible with --undo. A file that is edited
is backed up beside itself first (`.kilix-needle.bak`) and, for TOML and
JSON, parsed after the edit; a file that no longer parses is restored.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tomllib

NAME = "kilix-needle"
HOME = Path.home()
BIN = HOME / ".local" / "bin" / NAME
SCRIPT = Path(__file__).resolve().parent / NAME
BEGIN, END = f"# >>> {NAME} >>>", f"# <<< {NAME} <<<"
SURFACES = ("command", "alias", "hotkey", "claude", "codex", "grok", "omp")


class SetupError(RuntimeError):
    pass


def _block(body: str) -> str:
    return f"{BEGIN}\n{body.rstrip()}\n{END}\n"


def _strip_block(text: str) -> str:
    start = text.find(BEGIN)
    if start < 0:
        return text
    end = text.find(END, start)
    if end < 0:
        raise SetupError(f"an unterminated {NAME} block; fix it by hand")
    end += len(END)
    if text[end:end + 1] == "\n":
        end += 1
    return text[:start] + text[end:]


def _edit(path: Path, body: str | None, parse=None, dry_run=False) -> str:
    """Put (or with body None, remove) our marked block in a text file."""
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    new = _strip_block(old)
    if body is not None and BEGIN in old:
        # Replace the block where it stands: re-appending it moved an
        # identical block past the user's own lines and reported "updated".
        start = old.find(BEGIN)
        new = new[:start] + _block(body) + new[start:]
    elif body is not None:
        new = new + ("" if not new or new.endswith("\n") else "\n") + _block(body)
    if new == old:
        return f"{path}: unchanged"
    if dry_run:
        return f"{path}: would {'update' if body is not None else 'remove the block from'} it"
    if parse is not None:
        try:
            parse(new)
        except Exception as error:
            raise SetupError(f"{path}: the edit would not parse ({error}); left unchanged")
    _write(path, new)
    return f"{path}: {'updated' if body is not None else 'block removed'}"


def _write(path: Path, text: str) -> None:
    """Replace a config file's text, keeping a symlink a symlink.

    A managed dotfile is often a symlink into a dotfiles repository, so the
    file it points to is edited (os.path.realpath: relative and chained
    links too), and the backup lands beside the link the user named, never
    inside that repository (reviews KN-05, KN-R2-05, KN-R2-11).
    """
    target = Path(os.path.realpath(path))
    if target.exists():
        shutil.copy2(target, path.with_name(path.name + f".{NAME}.bak"))
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + f".{NAME}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, target.stat().st_mode & 0o777 if target.exists() else 0o600)
    os.replace(tmp, target)


def _command(undo, dry_run):
    if undo:
        if BIN.is_symlink() and Path(os.readlink(BIN)) == SCRIPT:
            if not dry_run:
                BIN.unlink()
            return f"{BIN}: {'would remove' if dry_run else 'removed'}"
        return f"{BIN}: not ours, left alone"
    if BIN.is_symlink() and Path(os.readlink(BIN)) == SCRIPT:
        return f"{BIN}: unchanged"
    if BIN.exists() or BIN.is_symlink():
        raise SetupError(f"{BIN} exists and is not this checkout's; not replaced")
    if not dry_run:
        BIN.parent.mkdir(parents=True, exist_ok=True)
        BIN.symlink_to(SCRIPT)
    return f"{BIN}: {'would link' if dry_run else 'linked'} to {SCRIPT}"


def _alias(undo, dry_run):
    return _edit(HOME / ".bash_aliases", None if undo else f"alias kn='{NAME}'", dry_run=dry_run)


def _hotkey(undo, dry_run):
    config = os.environ.get("KILIX_CONFIG_HOME")
    if not config:
        raise SetupError("KILIX_CONFIG_HOME is not set; run setup inside Kilix")
    line = (f"map ctrl+alt+space launch --type=overlay --cwd=current "
            f"{BIN} --under-overlay")
    result = _edit(Path(config) / "kitty.conf", None if undo else line, dry_run=dry_run)
    return result + ("" if dry_run or "unchanged" in result else
                     " (reload the Kilix config, or restart Kilix, to use it)")


def _run(argv: list[str], dry_run: bool) -> str:
    if shutil.which(argv[0]) is None:
        return f"{argv[0]}: not installed, skipped"
    if dry_run:
        return "would run: " + " ".join(argv)
    done = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    if done.returncode != 0:
        raise SetupError(f"{' '.join(argv)}: {done.stderr.strip() or done.returncode}")
    return "ran: " + " ".join(argv)


def _claude(undo, dry_run):
    if undo:
        return _run(["claude", "mcp", "remove", "--scope", "user", NAME], dry_run)
    listed = subprocess.run(["claude", "mcp", "get", NAME], capture_output=True, text=True) \
        if shutil.which("claude") else None
    if listed is not None and listed.returncode == 0:
        return "claude: already registered"
    return _run(["claude", "mcp", "add", "--scope", "user", NAME, "--", str(BIN), "mcp"], dry_run)


def _codex(undo, dry_run):
    body = (f"[mcp_servers.{NAME}]\n"
            f"command = {json.dumps(str(BIN))}\n"
            'args = ["mcp"]\n'
            'env_vars = ["KITTY_LISTEN_ON", "KITTY_WINDOW_ID", "KILIX_DATA_HOME", '
            '"KILIX_CONTENT_ROOT", "KILIX_CONFIG_HOME"]\n')
    return _edit(HOME / ".codex" / "config.toml", None if undo else body,
                 parse=tomllib.loads, dry_run=dry_run)


def _grok(undo, dry_run):
    if undo:
        return _run(["grok", "mcp", "remove", "--scope", "user", NAME], dry_run)
    return _run(["grok", "mcp", "add", "--scope", "user", NAME, str(BIN), "--", "mcp"], dry_run)


def _omp(undo, dry_run):
    path = HOME / ".omp" / "agent" / "mcp.json"
    if not path.parent.is_dir():
        return f"{path.parent}: omp not set up, skipped"
    existed = path.exists()
    data = json.loads(path.read_text(encoding="utf-8")) if existed else {}
    servers = data.setdefault("mcpServers", {})
    wanted = {"command": str(BIN), "args": ["mcp"]}
    if undo:
        if NAME not in servers:
            return f"{path}: unchanged"
        del servers[NAME]
        # Byte-reversible where possible (reviews KN-R3-09, KN-R4-10): the
        # backup setup took before adding the entry holds the user's own
        # formatting, compared after the same setdefault ("{}" as well); a file
        # setup created is removed when nothing else was ever in it.
        backup = path.with_name(path.name + f".{NAME}.bak")
        created = path.with_name(path.name + f".{NAME}.created")
        if not dry_run:
            try:
                pristine = backup.read_text(encoding="utf-8")
                earlier = json.loads(pristine)
                earlier.setdefault("mcpServers", {})
                if earlier == data:
                    _write(path, pristine)
                    created.unlink(missing_ok=True)   # review R6 mutant O02: no stale marker
                    return f"{path}: removed mcpServers.{NAME}"
            except FileNotFoundError:
                # Only a file setup recorded creating (review KN-R5-08: a
                # missing .bak alone deleted a user's own "{}").
                if created.exists() and data == {"mcpServers": {}}:
                    os.unlink(os.path.realpath(path))
                    created.unlink()
                    return f"{path}: removed (setup had created it)"
            except (OSError, ValueError):
                pass
    elif servers.get(NAME) == wanted:
        return f"{path}: unchanged"
    else:
        servers[NAME] = wanted
    if dry_run:
        return f"{path}: would {'remove' if undo else 'set'} mcpServers.{NAME}"
    marker = path.with_name(path.name + f".{NAME}.created")
    if not existed and not undo:
        marker.write_text("", encoding="utf-8")
    if undo:
        marker.unlink(missing_ok=True)   # the file stays with the user's data: not ours now
    _write(path, json.dumps(data, indent=2) + "\n")
    return f"{path}: {'removed' if undo else 'set'} mcpServers.{NAME}"


ACTIONS = {"command": _command, "alias": _alias, "hotkey": _hotkey, "claude": _claude,
           "codex": _codex, "grok": _grok, "omp": _omp}


def setup(only=SURFACES, undo=False, dry_run=False) -> tuple[int, list[str]]:
    status, lines = 0, []
    for name in only:
        try:
            lines.append(f"{name:8} {ACTIONS[name](undo, dry_run)}")
        except (SetupError, OSError, subprocess.TimeoutExpired, ValueError) as error:
            lines.append(f"{name:8} FAILED: {error}")
            status = 1
    return status, lines
