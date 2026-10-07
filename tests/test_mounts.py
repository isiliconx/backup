from guestvault import platforms


def test_deleted_bind_mounts_only_skip_empty_directories(tmp_path):
    empty = tmp_path / "old browser"
    empty.mkdir()
    normal = tmp_path / "normal"
    normal.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    (data / "keep").write_text("original work")
    file = tmp_path / "mounted-file"
    file.write_text("keep this file too")
    mountinfo = tmp_path / "mountinfo"
    rows = []
    for target, root in [
        (empty, "/old//deleted"),
        (normal, "/old/deleted"),
        (data, "/old//deleted"),
        (file, "/old//deleted"),
    ]:
        escaped = str(target).replace(" ", r"\040")
        rows.append(f"10 5 8:1 {root} {escaped} rw - ext4 /dev/test rw")
    rows += ["invalid", "11 5 8:1 /old//deleted / rw - ext4 /dev/test rw"]
    mountinfo.write_text("\n".join(rows))
    assert platforms.deleted_directory_mounts(mountinfo) == [str(empty)]
    assert platforms.deleted_directory_mounts(tmp_path / "missing") == []


def test_deleted_mount_exclusions_escape_glob_characters(tmp_path, monkeypatch, restic_binary):
    from guestvault.engine import Engine, configure
    from guestvault.storage import Store

    source = tmp_path / "source"
    source.mkdir()
    empty = source / "profile[old]*?"
    empty.mkdir()
    keep = source / "profileo-data"
    keep.write_text("preserve the neighbouring original file")
    monkeypatch.setattr(platforms, "deleted_directory_mounts", lambda: [str(empty)])
    excluded = platforms.system_excludes()
    # The disposable source is under /tmp, which real system backups omit.
    monkeypatch.setattr(platforms, "system_excludes", lambda: [p for p in excluded if p != "/tmp"])
    monkeypatch.setattr(platforms, "system_sources", lambda: [str(source)])
    monkeypatch.setattr(platforms, "elevated", lambda: True)
    store = Store(tmp_path / "state")
    configure(store, "system", [], tmp_path / "repository", password_required=False)
    engine = Engine(store, restic_binary)
    result = engine.backup("")
    target = tmp_path / "recovered"
    engine.restore_repository(str(tmp_path / "repository"), result["snapshot"], target, "")
    assert next(target.rglob(keep.name)).read_text() == keep.read_text()
    assert not any(p.name == empty.name for p in target.rglob("*"))
