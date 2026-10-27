from claude_phone import registry


def test_lowest_free_extension():
    a = registry.register("a", "repo-a", "main", None, "/a", "tab-a")
    b = registry.register("b", "repo-b", "main", None, "/b", "tab-b")
    assert (a.ext, b.ext) == (301, 302)
    registry.unregister("a")
    assert registry.register("c", "repo-c", "main", None, "/c", "tab-c").ext == 301


def test_new_session_in_same_tab_takes_extension_and_voicemail():
    old = registry.register("old", "repo", "main", None, "/r", "tab")
    registry.add_voicemail("old", old.ext, "repo/main", "Ship it?", None, None)
    new = registry.register("new", "repo", "main", None, "/r", "tab")
    assert new.ext == old.ext
    assert registry.find("old") is None
    assert [v.session_id for v in registry.pending_voicemails()] == ["new"]


def test_find_falls_back_to_tab():
    registry.register("live", "repo", "main", None, "/r", "tab")
    assert registry.find("stale-id", "tab").session_id == "live"


def test_remove_missing():
    registry.register("a", "repo-a", "main", None, "/a", "tab-a")
    registry.register("b", "repo-b", "main", None, "/b", "tab-b")
    gone = registry.remove_missing({"tab-b"})
    assert [s.session_id for s in gone] == ["a"]
    assert [s.session_id for s in registry.all_sessions()] == ["b"]
