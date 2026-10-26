from driftless.commands import Command, CommandName, MemoryCommands, parse_command
from driftless.memory import Memory, RememberedChange
from driftless.pr_writer import PublishAction
from tests.test_premerge import FakeCheckRepo
from tests.test_pr_writer import REPO


def test_parse_commands() -> None:
    assert parse_command("  /driftless apply \n") == Command(name=CommandName.APPLY)
    assert parse_command("/driftless ignore `docs/legacy/`\nbecause it's frozen") == Command(
        name=CommandName.IGNORE, argument="docs/legacy/"
    )
    assert parse_command('/driftless style "use British spelling"') == Command(
        name=CommandName.STYLE, argument="use British spelling"
    )
    assert parse_command("/driftless Memory") == Command(name=CommandName.MEMORY)
    for body in ("/driftless apply please", "/driftless ignore", "/driftless memory x", "/driftless forget", "LGTM",
                 "/driftlessapply", "please /driftless apply"):
        assert parse_command(body) is None, body


def run(memory: Memory, gh: FakeCheckRepo, text: str) -> str:
    command = parse_command(text)
    assert command is not None
    MemoryCommands(gh, memory).run(REPO, 7, "maintainer", command)
    return gh.comments[len(gh.comments)].body


def test_memory_commands_change_and_show_the_repo_memory() -> None:
    memory, gh = Memory("sqlite://"), FakeCheckRepo()

    reply = run(memory, gh, "/driftless ignore CHANGELOG.md")
    assert reply.startswith("🧠 @maintainer Driftless will ignore `CHANGELOG.md` in this repo")
    assert "`/driftless unignore CHANGELOG.md`" in reply
    assert "already ignored" in run(memory, gh, "/driftless ignore CHANGELOG.md")
    assert "noted style preference #1: “use British spelling”" in run(memory, gh, "/driftless style use British spelling")
    run(memory, gh, "/driftless style keep examples minimal")
    memory.record_docs_pr(REPO, 12, 7, [RememberedChange(file="README.md", section="Config", kind="updated", reason="r")])
    memory.resolve_docs_pr(REPO, 12, merged=False)

    shown = run(memory, gh, "/driftless memory")
    assert f"🧠 **What Driftless remembers for {REPO}**" in shown
    assert "- `CHANGELOG.md`" in shown and "1. use British spelling\n2. keep examples minimal" in shown
    assert "0 merged, 1 closed without merging" in shown and "- `README.md` › Config (updated, docs PR #12)" in shown

    assert "removed style preference #1: “use British spelling”" in run(memory, gh, "/driftless unstyle 1")
    assert "no style preference #9" in run(memory, gh, "/driftless unstyle 9")
    assert "no longer ignores `CHANGELOG.md`" in run(memory, gh, "/driftless unignore CHANGELOG.md")
    assert "no ignore rule `CHANGELOG.md` (current rules: none)" in run(memory, gh, "/driftless unignore CHANGELOG.md")
    assert memory.style_notes(REPO) == ["keep examples minimal"] and memory.ignore_rules(REPO) == []


def test_memory_commands_need_write_access() -> None:
    memory = Memory("sqlite://")
    for permission in ("read", "triage", "none"):
        gh = FakeCheckRepo(permission=permission)
        published = MemoryCommands(gh, memory).run(REPO, 7, "drive-by", Command(name=CommandName.IGNORE, argument="README.md"))
        assert published.action == PublishAction.DECLINED
        assert "only users with write access" in gh.comments[1].body
    assert memory.ignore_rules(REPO) == []
