def test_old_session_store_names_are_not_public():
    import glyff
    import glyff.store
    import glyff_file_store
    import glyff_sqlite

    assert not hasattr(glyff, "SessionStore")
    assert not hasattr(glyff.store, "MemorySessionStore")
    assert not hasattr(glyff_file_store, "JsonFileSessionStore")
    assert not hasattr(glyff_sqlite, "SQLiteSessionStore")


def test_new_backend_names_are_public():
    import glyff.store
    import glyff_file_store
    import glyff_sqlite

    assert hasattr(glyff.store, "MemoryBackend")
    assert hasattr(glyff_file_store, "JsonFileBackend")
    assert hasattr(glyff_sqlite, "SQLiteBackend")
