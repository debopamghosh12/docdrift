"""`/driftless <command>` PR comments, and the memory commands' replies.

`/driftless apply` commits doc fixes (premerge.py). The rest change or show what Driftless
remembers for the repo: `ignore` / `unignore <file or heading>`, `style <note>`,
`unstyle <n>`, `memory`. Every command needs write access, like `apply`.
"""

from enum import Enum
from typing import Protocol

from pydantic import BaseModel

from driftless.memory import Memory
from driftless.pr_body import FOOTER
from driftless.pr_writer import PublishAction, PublishResult
from driftless.premerge import WRITE_PERMISSIONS

PREFIX = "/driftless"
MAX_ARGUMENT_CHARS = 300


class CommandName(str, Enum):
    APPLY = "apply"
    IGNORE = "ignore"
    UNIGNORE = "unignore"
    STYLE = "style"
    UNSTYLE = "unstyle"
    MEMORY = "memory"


TAKES_ARGUMENT = frozenset({CommandName.IGNORE, CommandName.UNIGNORE, CommandName.STYLE, CommandName.UNSTYLE})


class Command(BaseModel):
    name: CommandName
    argument: str = ""

    @property
    def text(self) -> str:
        return f"{PREFIX} {self.name.value}" + (f" {self.argument}" if self.argument else "")


def parse_command(body: str) -> Command | None:
    """The command on a comment's first line; None for anything else (unknown, or missing/extra argument)."""
    line = body.strip().split("\n", 1)[0].strip()
    head, _, rest = line.partition(" ")
    if head != PREFIX:
        return None
    verb, _, argument = rest.strip().partition(" ")
    try:
        name = CommandName(verb.lower())
    except ValueError:
        return None
    argument = _unquote(argument.strip())
    if (name in TAKES_ARGUMENT) != bool(argument) or len(argument) > MAX_ARGUMENT_CHARS:
        return None
    return Command(name=name, argument=argument)


class CommentRepo(Protocol):
    def permission(self, repo: str, user: str) -> str: ...

    def create_comment(self, repo: str, number: int, body: str) -> str: ...


class MemoryCommands:
    def __init__(self, github: CommentRepo, memory: Memory) -> None:
        self._gh = github
        self._memory = memory

    def run(self, repo: str, number: int, user: str, command: Command) -> PublishResult:
        if self._gh.permission(repo, user) not in WRITE_PERMISSIONS:
            reply = f"⚠️ @{user} only users with write access to this repository can change or read what Driftless remembers."
            return self._reply(repo, number, reply, PublishAction.DECLINED)
        return self._reply(repo, number, self.respond(repo, user, command), PublishAction.COMMENTED)

    def respond(self, repo: str, user: str, command: Command) -> str:
        """Carry out the command and say what changed (overridden in Action mode)."""
        handler = {
            CommandName.IGNORE: self._ignore,
            CommandName.UNIGNORE: self._unignore,
            CommandName.STYLE: self._style,
            CommandName.UNSTYLE: self._unstyle,
            CommandName.MEMORY: self._show,
        }[command.name]
        return handler(repo, user, command.argument)

    def _ignore(self, repo: str, user: str, rule: str) -> str:
        if not self._memory.add_ignore(repo, rule, user):
            return f"🧠 @{user} `{rule}` is already ignored in this repo."
        return (
            f"🧠 @{user} Driftless will ignore `{rule}` in this repo: matching files or sections (and their "
            f"subsections) won't be reviewed or rewritten, and a matching file's examples won't be run. "
            f"Undo with `{PREFIX} unignore {rule}`."
        )

    def _unignore(self, repo: str, user: str, rule: str) -> str:
        if self._memory.remove_ignore(repo, rule):
            return f"🧠 @{user} Driftless no longer ignores `{rule}` in this repo."
        rules = self._memory.ignore_rules(repo)
        current = ", ".join(f"`{r}`" for r in rules) if rules else "none"
        return f"🧠 @{user} there is no ignore rule `{rule}` (current rules: {current})."

    def _style(self, repo: str, user: str, note: str) -> str:
        n = self._memory.add_style(repo, note, user)
        return (
            f"🧠 @{user} noted style preference #{n}: “{note}”. Driftless follows it whenever it writes docs "
            f"in this repo. Remove it with `{PREFIX} unstyle {n}`."
        )

    def _unstyle(self, repo: str, user: str, argument: str) -> str:
        removed = self._memory.remove_style(repo, int(argument)) if argument.isdigit() else None
        if removed is None:
            return f"🧠 @{user} there is no style preference #{argument}; `{PREFIX} memory` lists them with their numbers."
        return f"🧠 @{user} removed style preference #{argument}: “{removed}”."

    def _show(self, repo: str, user: str, _: str) -> str:
        return render_memory(repo, self._memory)

    def _reply(self, repo: str, number: int, text: str, action: PublishAction) -> PublishResult:
        url = self._gh.create_comment(repo, number, f"{text}\n\n---\n{FOOTER}\n")
        return PublishResult(action=action, url=url)


def render_memory(repo: str, memory: Memory) -> str:
    rules, notes = memory.ignore_rules(repo), memory.style_notes(repo)
    counts, rejected = memory.feedback_counts(repo), memory.recent_rejected(repo)
    lines = [f"🧠 **What Driftless remembers for {repo}**", ""]
    lines.append(f"**Ignored** (`{PREFIX} unignore <file or heading>` to remove)")
    lines += [f"- `{rule}`" for rule in rules] or ["- nothing"]
    lines += ["", f"**Style preferences** (`{PREFIX} unstyle <n>` to remove)"]
    lines += [f"{i}. {note}" for i, note in enumerate(notes, 1)] or ["- none"]
    lines += [
        "",
        f"**Feedback from docs PRs:** {counts['accepted']} merged, {counts['rejected']} closed without merging.",
    ]
    if rejected:
        lines.append("Most recent rejected changes, shown to the model as what not to do:")
        lines += [f"- `{c.file}` › {c.section} ({c.kind}, docs PR #{c.docs_pr})" for c in rejected]
    return "\n".join(lines)


def _unquote(text: str) -> str:
    for left, right in (("`", "`"), ('"', '"'), ("'", "'"), ("“", "”")):
        if len(text) >= 2 and text.startswith(left) and text.endswith(right):
            return text[1:-1].strip()
    return text
