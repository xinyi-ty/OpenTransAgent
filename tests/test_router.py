from __future__ import annotations

import pytest

from config.router import _cpp_to_py_ext, _py_to_cpp_ext, get_effective_route, get_route, validate_pair


def test_get_route_supports_language_aliases() -> None:
    route = get_route("C++", "py")

    assert route is not None
    assert route.pair == ("cpp", "python")


def test_validate_pair_supports_language_aliases() -> None:
    assert validate_pair("py", "c++") is True


def test_unknown_explicit_route_returns_none_but_known_pair_has_effective_route() -> None:
    assert get_route("go", "rust") is None
    route = get_effective_route("go", "rust")
    assert route is not None
    assert route.explicit is False
    assert route.file_extension_map("src/main.go") == "src/main.rs"
    assert validate_pair("go", "rust") is True


def test_unknown_language_pair_is_rejected() -> None:
    assert get_route("brainfuck", "whitespace") is None
    assert get_effective_route("brainfuck", "whitespace") is None
    assert validate_pair("brainfuck", "whitespace") is False


def test_cpp_to_python_extension_map_matches_cpp_source_extensions() -> None:
    for filename in ["foo.h", "foo.hpp", "foo.hxx", "foo.cpp", "foo.cxx", "foo.cc"]:
        assert _cpp_to_py_ext(filename) == "foo.py"


def test_cpp_to_python_extension_map_is_case_insensitive() -> None:
    assert _cpp_to_py_ext("include/Foo.HPP") == "include/Foo.py"


def test_python_to_cpp_extension_map() -> None:
    assert _py_to_cpp_ext("pkg/foo.py") == "pkg/foo.cpp"
    assert _py_to_cpp_ext("pkg/foo.txt") == "pkg/foo.txt"


def test_effective_route_keeps_explicit_cpp_python_overrides() -> None:
    cpp_to_py = get_effective_route("c++", "py")
    py_to_cpp = get_effective_route("py", "cplusplus")

    assert cpp_to_py is not None
    assert cpp_to_py.explicit is True
    assert cpp_to_py.file_extension_map("src/foo.hpp") == "src/foo.py"
    assert cpp_to_py.file_extension_map("src/foo.CXX") == "src/foo.py"
    assert py_to_cpp is not None
    assert py_to_cpp.explicit is True
    assert py_to_cpp.file_extension_map("pkg/foo.py") == "pkg/foo.cpp"


def test_python_to_cpp_route_prefers_local_gtest_and_ctest() -> None:
    route = get_route("python", "cpp")
    assert route is not None

    text = route.prompt_env_restriction
    assert 'cmake -S . -B build -G "MinGW Makefiles"' in text
    assert "ctest --test-dir build" in text
    assert "D:/googletest" in text
    assert "network FetchContent is a last resort only" in text
    assert "CMakeLists.txt and tests are" in text
    assert "public_tests/*, tests/*, or test/*" in text
    assert "auto-downloaded by FetchContent" not in text


def test_routes_are_frozen() -> None:
    route = get_route("cpp", "python")
    assert route is not None

    with pytest.raises(Exception):
        route.prompt_pair_instruction = "changed"
