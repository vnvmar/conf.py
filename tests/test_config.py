from __future__ import annotations

import json
import pathlib
import typing

import pytest
from pydantic import BaseModel

from conf import ConfValue, Config, environ


def write(path: pathlib.Path, content: str) -> pathlib.Path:
    _ = path.write_text(content)
    return path


def test_dotenv_values_and_scopes(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FOO", raising=False)
    monkeypatch.delenv("SERVICE_DATABASE_URL", raising=False)
    path = write(tmp_path / ".env", "FOO=bar\nSERVICE_DATABASE_URL=postgres://localhost/app\n")

    config = Config(path)

    assert config.FOO == "bar"
    assert isinstance(config.FOO, ConfValue)
    assert config.SERVICE.DATABASE.URL == "postgres://localhost/app"
    assert config.maybe.MISSING == None
    assert not config.maybe.MISSING.DEEP.VALUE
    with pytest.raises(KeyError, match="MISSING"):
        config.MISSING


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("config.json", json.dumps({"service": {"api-key": "secret"}, "ports": [8000, 8001]})),
        ("config.yaml", "service:\n  api-key: secret\nports:\n  - 8000\n  - 8001\n"),
        ("config.yml", "service:\n  api-key: secret\nports:\n  - 8000\n  - 8001\n"),
        ("config.toml", "ports = [8000, 8001]\n[service]\napi-key = \"secret\"\n"),
    ],
)
def test_structured_formats_support_nested_access_and_lists(
    tmp_path: pathlib.Path,
    filename: str,
    content: str,
) -> None:
    config = Config(write(tmp_path / filename, content))

    assert config.SERVICE.API_KEY == "secret"
    assert config.PORTS.to(list) == ["8000", "8001"]
    assert config.PORTS.to(object) == [8000, 8001]
    assert config.maybe.SERVICE.MISSING == None


def test_conf_value_helpers() -> None:
    assert ConfValue("value").to(str) == "value"
    assert ConfValue("1").to(int) == 1
    assert ConfValue("1.5").to(float) == 1.5
    assert ConfValue("yes").to(bool) is True
    assert ConfValue("off").to(bool) is False
    assert ConfValue("a, b").to(list) == ["a", "b"]
    assert ConfValue("a; b").to(list, sep=";") == ["a", "b"]
    assert ConfValue('["a", 2]').to(list) == ["a", "2"]
    assert ConfValue('{"a": 1}').to(dict) == {"a": 1}
    assert ConfValue('{"a": 1}').to(object) == {"a": 1}
    assert ConfValue("x").to(lambda value: value.upper()) == "X"
    assert ConfValue("debug").one_of("debug", "info") == "debug"
    assert ConfValue("warn").one_of("debug", "info", None) is None
    assert (ConfValue("warn") & ("debug", "info", None)) is None

    with pytest.raises(ValueError):
        _ = ConfValue("maybe").to(bool)
    with pytest.raises(ValueError):
        _ = ConfValue("[1, 2]").to(dict)
    with pytest.raises(TypeError):
        _ = ConfValue("value").to(None)
    with pytest.raises(ValueError):
        _ = ConfValue("warn") & ("debug", "info")


def test_pydantic_loading_from_flat_config(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Database(BaseModel):
        host: str
        port: int

    class AppConfig(BaseModel):
        debug: bool
        database: Database
        optional_token: str | None

    monkeypatch.delenv("DEBUG", raising=False)
    monkeypatch.delenv("DATABASE_HOST", raising=False)
    monkeypatch.delenv("DATABASE_PORT", raising=False)
    path = write(tmp_path / ".env", "DEBUG=true\nDATABASE_HOST=localhost\nDATABASE_PORT=5432\n")

    model = Config(path).load(AppConfig)

    assert model.debug is True
    assert model.database.host == "localhost"
    assert model.database.port == 5432
    assert model.optional_token is None


def test_pydantic_loading_from_structured_config(tmp_path: pathlib.Path) -> None:
    class Worker(BaseModel):
        name: str
        retries: int

    class AppConfig(BaseModel):
        workers: list[Worker]
        token: str | None

    path = write(
        tmp_path / "config.json",
        json.dumps({"workers": [{"name": "alpha", "retries": 2}], "extra": "ignored"}),
    )

    model = Config(path).load(AppConfig)

    assert model.workers == [Worker(name="alpha", retries=2)]
    assert model.token is None


def test_load_requires_pydantic_model(tmp_path: pathlib.Path) -> None:
    path = write(tmp_path / "config.json", "{}")
    invalid_model = typing.cast(type[BaseModel], dict)

    with pytest.raises(TypeError, match="Config.load"):
        _ = Config(path).load(invalid_model)


def test_file_errors(tmp_path: pathlib.Path) -> None:
    with pytest.raises(FileNotFoundError):
        _ = Config(tmp_path / "missing.json")

    with pytest.raises(ValueError, match="Unsupported file type"):
        _ = Config(write(tmp_path / "config.ini", "A=1\n"))

    with pytest.raises(ValueError, match="Unsupported file type"):
        _ = Config(write(tmp_path / "config.py", 'VALUE = "1"\n'))


@pytest.mark.parametrize(
    ("filename", "content", "message"),
    [
        ("bad.json", "[]", "JSON"),
        ("bad.yaml", "- value\n", "YAML"),
        ("bad.toml", "1", "TOML"),
    ],
)
def test_structured_loaders_require_top_level_mapping(
    tmp_path: pathlib.Path,
    filename: str,
    content: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _ = Config(write(tmp_path / filename, content))
