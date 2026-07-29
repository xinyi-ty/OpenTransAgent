from __future__ import annotations

import pytest

from config.router import _cpp_to_py_ext, _py_to_cpp_ext, get_route, validate_pair


def test_get_route_supports_language_aliases() -> None:
    route = get_route("C++", "py")

    assert route is not None
    assert route.pair == ("cpp", "python")


def test_validate_pair_supports_language_aliases() -> None:
    assert validate_pair("py", "c++") is True


def test_unknown_route_returns_none() -> None:
    assert get_route("go", "rust") is None
    assert validate_pair("go", "rust") is False


def test_cpp_to_python_extension_map_matches_cpp_source_extensions() -> None:
    for filename in ["foo.h", "foo.hpp", "foo.hxx", "foo.cpp", "foo.cxx", "foo.cc"]:
        assert _cpp_to_py_ext(filename) == "foo.py"


def test_cpp_to_python_extension_map_is_case_insensitive() -> None:
    assert _cpp_to_py_ext("include/Foo.HPP") == "include/Foo.py"


def test_python_to_cpp_extension_map() -> None:
    assert _py_to_cpp_ext("pkg/foo.py") == "pkg/foo.cpp"
    assert _py_to_cpp_ext("pkg/foo.txt") == "pkg/foo.txt"


def test_routes_are_frozen() -> None:
    route = get_route("cpp", "python")
    assert route is not None

    with pytest.raises(Exception):
        route.prompt_pair_instruction = "changed"
