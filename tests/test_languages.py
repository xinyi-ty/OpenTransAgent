from __future__ import annotations

from config.languages import (
    LANGUAGE_CONFIG,
    get_all_code_extensions,
    get_source_extensions,
    get_target_extensions,
    get_test_extensions,
    get_test_unit_label,
    normalize_language,
    should_refresh_precheck_after_test_copy,
)


def test_get_target_extensions_returns_copy() -> None:
    exts = get_target_extensions("python")
    exts.append(".tmp")

    assert LANGUAGE_CONFIG["python"]["target_exts"] == [".py"]
    assert get_target_extensions("python") == [".py"]


def test_get_source_extensions_returns_copy() -> None:
    exts = get_source_extensions("cpp")
    exts.clear()

    assert ".cpp" in LANGUAGE_CONFIG["cpp"]["source_exts"]
    assert ".cpp" in get_source_extensions("cpp")


def test_language_aliases_are_supported_for_extensions() -> None:
    assert normalize_language("C++") == "cpp"
    assert get_target_extensions("c++") == [".cpp", ".h", ".hpp"]
    assert get_source_extensions("py") == [".py"]
    assert get_target_extensions("C#") == [".cs"]


def test_unknown_language_fallback_is_normalized() -> None:
    assert get_target_extensions("kotlin") == [".kotlin"]
    assert get_source_extensions(".zig") == [".zig"]
    assert get_target_extensions("  ") == [".txt"]


def test_all_configured_languages_have_valid_extensions() -> None:
    for language, cfg in LANGUAGE_CONFIG.items():
        for key in ("source_exts", "target_exts"):
            exts = cfg[key]
            assert exts
            assert len(exts) == len(set(exts))
            assert all(ext.startswith(".") for ext in exts)
        assert get_source_extensions(language) == list(cfg["source_exts"])
        assert get_target_extensions(language) == list(cfg["target_exts"])


def test_shared_code_extensions_include_all_configured_extensions() -> None:
    all_exts = get_all_code_extensions()

    for cfg in LANGUAGE_CONFIG.values():
        for key in ("source_exts", "target_exts", "test_exts"):
            for ext in cfg.get(key, []):
                assert ext in all_exts


def test_test_metadata_is_language_config_driven() -> None:
    assert get_test_extensions("c++") == [".cpp", ".cxx", ".cc"]
    assert get_test_unit_label("cplusplus") == "CTest targets"
    assert should_refresh_precheck_after_test_copy("cc") is True
    assert should_refresh_precheck_after_test_copy("python") is False
