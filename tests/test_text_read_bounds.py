"""Text preview pins one file and enforces its cap on actual read bytes."""

from pathlib import Path

import pytest


@pytest.fixture
def observe_text_reads(app_module, monkeypatch):
    from backend import files

    def observe(target, mutate=lambda: None):
        opened = []
        read_bytes = []
        read_sizes = []
        mutated = False
        path_open = Path.open
        response_open = files._open_response_file

        class ObservedFile:
            def __init__(self, raw):
                self.raw = raw
                opened.append(self)

            def read(self, size=-1):
                nonlocal mutated
                if not mutated:
                    mutated = True
                    mutate()
                chunk = self.raw.read(size)
                read_sizes.append(size)
                read_bytes.append(len(chunk.encode("utf-8") if isinstance(chunk, str) else chunk))
                return chunk

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                self.raw.close()

            def __getattr__(self, name):
                return getattr(self.raw, name)

        def open_path(path, *args, **kwargs):
            stream = path_open(path, *args, **kwargs)
            mode = args[0] if args else kwargs.get("mode", "r")
            if path == target and "r" in mode:
                return ObservedFile(stream)
            return stream

        def open_response(path):
            stream, info = response_open(path)
            return (ObservedFile(stream) if path == target else stream), info

        # Both hooks delegate real I/O: the path hook observes the old reader,
        # the descriptor hook observes its replacement. Mutation starts only
        # when an opened file is about to be read, after its size snapshot.
        monkeypatch.setattr(Path, "open", open_path)
        monkeypatch.setattr(files, "_open_response_file", open_response)
        return opened, read_bytes, read_sizes

    return observe


def test_multibyte_growth_respects_byte_limit(
    client, auth, temp_root, observe_text_reads,
):
    from backend import files

    target = temp_root / "growing.txt"
    target.write_bytes(b"initial\n")
    payload = "文".encode("utf-8") * (files.MAX_TEXT_SIZE // 3 + 1)

    def grow():
        with target.open("ab") as output:
            output.write(payload)

    opened, read_bytes, _ = observe_text_reads(target, grow)
    response = client.get("/api/files/read", params={"path": target.name}, headers=auth)
    assert response.status_code == 413
    assert opened and all(stream.closed for stream in opened)
    assert sum(read_bytes) <= files.MAX_TEXT_SIZE + 1


def test_growth_does_not_read_the_entire_oversized_file(
    client, auth, temp_root, observe_text_reads,
):
    from backend import files

    target = temp_root / "growing.txt"
    target.write_bytes(b"initial\n")

    def grow():
        with target.open("ab") as output:
            output.write(b"x" * (files.MAX_TEXT_SIZE * 4))

    opened, read_bytes, read_sizes = observe_text_reads(target, grow)
    response = client.get("/api/files/read", params={"path": target.name}, headers=auth)
    assert response.status_code == 413
    assert opened and all(stream.closed for stream in opened)
    assert sum(read_bytes) <= files.MAX_TEXT_SIZE + 1, read_bytes
    assert all(size >= 0 for size in read_sizes), "preview performed an unbounded read"


@pytest.mark.parametrize("replacement", ["sensitive_symlink", "atomic_replace", "unlink"])
def test_text_read_keeps_opened_file_after_path_changes(
    client, auth, temp_root, observe_text_reads, replacement,
):
    target = temp_root / "changing.txt"
    original = b"ORIGINAL_PUBLIC_CONTENT\n"
    target.write_bytes(original)
    alternate = temp_root / ".env"
    alternate.write_bytes(b"SYNTHETIC_PRIVATE_REPLACEMENT\n")

    def replace():
        if replacement == "sensitive_symlink":
            target.unlink()
            target.symlink_to(alternate)
        elif replacement == "atomic_replace":
            alternate.replace(target)
        else:
            target.unlink()

    opened, _, _ = observe_text_reads(target, replace)
    response = client.get("/api/files/read", params={"path": target.name}, headers=auth)
    assert response.status_code == 200
    assert response.content == original
    assert len(opened) == 1
    assert all(stream.closed for stream in opened)


@pytest.mark.parametrize("kind", ["binary", "oversize"])
def test_rejected_text_closes_file_without_reading_its_entire_body(
    client, auth, temp_root, observe_text_reads, kind,
):
    from backend import files

    target = temp_root / "rejected.txt"
    payload = (b"text\x00" + b"x" * files.SNIFF_BYTES * 2
               if kind == "binary" else b"x" * (files.MAX_TEXT_SIZE + 1))
    target.write_bytes(payload)
    opened, read_bytes, _ = observe_text_reads(target)
    response = client.get("/api/files/read", params={"path": target.name}, headers=auth)
    assert response.status_code == (415 if kind == "binary" else 413)
    assert len(opened) == 1 and opened[0].closed
    assert sum(read_bytes) == (files.SNIFF_BYTES if kind == "binary" else 0)


@pytest.mark.parametrize("raw,expected", [
    (b"one\r\ntwo\rthree\n", "one\ntwo\nthree\n"),
    (b"readable text \xff ending\n", "readable text \ufffd ending\n"),
])
def test_text_decoding_preserves_replacement_and_universal_newlines(
    client, auth, temp_root, observe_text_reads, raw, expected,
):
    target = temp_root / "decoded.txt"
    target.write_bytes(raw)
    opened, read_bytes, _ = observe_text_reads(target)
    response = client.get("/api/files/read", params={"path": target.name}, headers=auth)
    assert response.status_code == 200
    assert response.text == expected
    assert len(opened) == 1 and opened[0].closed
    assert sum(read_bytes) == len(raw)
    assert response.headers["content-disposition"] == "inline"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_text_exact_byte_limit_is_readable(
    client, auth, temp_root, observe_text_reads,
):
    from backend import files

    payload = ("文".encode("utf-8") * (files.MAX_TEXT_SIZE // 3)
               + b"x" * (files.MAX_TEXT_SIZE % 3))
    target = temp_root / "at-limit.txt"
    target.write_bytes(payload)
    opened, read_bytes, _ = observe_text_reads(target)
    response = client.get("/api/files/read", params={"path": target.name}, headers=auth)
    assert response.status_code == 200
    assert response.content == payload
    assert len(opened) == 1 and opened[0].closed
    assert sum(read_bytes) == files.MAX_TEXT_SIZE
