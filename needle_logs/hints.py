"""Routing help for journal requests accidentally sent to the logs reader."""
import re


def journal_hint(name=None):
    # A query is literal text, not necessarily a program name. Only copy a
    # bounded single tag into the example; paths, pane IDs and prose stay out.
    tag = name if (isinstance(name, str) and
                   re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@:-]{0,127}", name) and
                   not name.isdecimal() and not name.endswith(".") and
                   name.lower() not in ("the", "this", "that", "it")) else "NAME"
    return ("For system-journal errors, call kilix_system_read with request: "
            f'"errors from the program {tag} in the last 15 minutes". '
            "Use the intended program tag and time window; this is an example, "
            "no journal entries were read.")
