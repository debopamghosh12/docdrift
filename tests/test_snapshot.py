import io
import tarfile

from driftless.snapshot import read_tarball


def make_tarball(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path, data in files.items():
            info = tarfile.TarInfo(f"owner-repo-abc123/{path}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def test_reads_docs_and_sources_and_strips_prefix() -> None:
    data = make_tarball(
        {
            "README.md": b"# Hi",
            "docs/guide.md": b"guide",
            "app/main.py": b"x = 1",
            "logo.png": b"\x89PNG",
            "src/latin1.py": "caf\u00e9".encode("latin-1"),
            "requirements.txt": b"fastapi",
            "notes.txt": b"scratch",
        }
    )
    assert read_tarball(data) == {
        "README.md": "# Hi",
        "docs/guide.md": "guide",
        "app/main.py": "x = 1",
        "requirements.txt": "fastapi",
    }
