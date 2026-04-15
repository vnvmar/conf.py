"""Small configuration loader with typed helpers and scoped access."""

from __future__ import annotations

import ast
import json
import os
import pathlib
import types
import typing
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence

import dotenv
from pydantic import BaseModel

__all__ = ["Config", "ConfValue", "environ"]


_ConfigData: typing.TypeAlias = dict[str, object]


def _json_loads(value: str) -> object:
    return typing.cast(object, json.loads(value))


def _validate_one_of(value: str, values: Sequence[str | None]) -> "ConfValue | None":
    nullable = None in values
    allowed = frozenset(item for item in values if item is not None)

    if value in allowed:
        return ConfValue(value)
    if nullable:
        return None

    choices = " | ".join(f'"{item}"' for item in sorted(allowed))
    raise ValueError(f"Value '{value}' is not one of the allowed choices: {choices}.")


class ConfValue(str):
    """String value returned from configuration lookups."""

    @typing.overload
    def to(self, target: type[str], *, sep: str = ",") -> str: ...

    @typing.overload
    def to(self, target: type[bool], *, sep: str = ",") -> bool: ...

    @typing.overload
    def to(self, target: type[int], *, sep: str = ",") -> int: ...

    @typing.overload
    def to(self, target: type[float], *, sep: str = ",") -> float: ...

    @typing.overload
    def to(self, target: type[list[object]], *, sep: str = ",") -> list[str]: ...

    @typing.overload
    def to(self, target: type[dict[str, object]], *, sep: str = ",") -> dict[str, object]: ...

    @typing.overload
    def to(self, target: type[object], *, sep: str = ",") -> object: ...

    @typing.overload
    def to[_T](self, target: Callable[[str], _T], *, sep: str = ",") -> _T: ...

    @typing.overload
    def to(self, target: object, *, sep: str = ",") -> object: ...

    def to(self, target: object, *, sep: str = ",") -> object:
        if target is str:
            return str(self)
        if target is bool:
            return self._to_bool()
        if target is list:
            return self._to_list(sep=sep)
        if target is dict:
            parsed = _json_loads(self)
            if not isinstance(parsed, dict):
                raise ValueError(f"Cannot parse '{self}' as dict.")
            return typing.cast(dict[str, object], parsed)
        if target is object:
            return _json_loads(self)
        if not callable(target):
            raise TypeError("ConfValue.to() expects a type or callable converter.")
        converter = typing.cast(Callable[[str], object], target)
        return converter(str(self))

    def _to_list(self, sep: str = ",") -> list[str]:
        stripped = self.strip()
        if stripped.startswith("["):
            try:
                parsed = _json_loads(stripped)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                return [str(item) for item in typing.cast(list[object], parsed)]
        return [item.strip() for item in self.split(sep)]

    def _to_bool(self) -> bool:
        value = self.lower()
        if value in ("1", "true", "yes", "on"):
            return True
        if value in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"Cannot parse '{self}' as boolean.")

    def one_of(self, *values: str | None) -> "ConfValue | None":
        return _validate_one_of(self, values)

    def __and__(self, values: Sequence[str | None]) -> "ConfValue | None":
        return _validate_one_of(self, values)

    def __getattr__(self, name: str) -> "ConfValue":
        raise AttributeError(f"'{self}' is a configuration value, not a namespace. Cannot access '{name}' on it.")


def _normalise_key(name: str) -> str:
    return name.replace("-", "_").upper()


def _normalise_value(value: object) -> str:
    if isinstance(value, list):
        return json.dumps(value)
    return str(value)


def _normalise_data(data: object) -> object:
    if _is_string_dict(data):
        return {_normalise_key(key): _normalise_data(value) for key, value in data.items()}
    if isinstance(data, list):
        return [_normalise_data(item) for item in typing.cast(list[object], data)]
    return data


def _is_string_dict(data: object) -> typing.TypeGuard[_ConfigData]:
    if not isinstance(data, dict):
        return False
    mapping = typing.cast(dict[object, object], data)
    return all(isinstance(key, str) for key in mapping)


class _Loader(ABC):
    @property
    def flat(self) -> bool:
        return False

    @abstractmethod
    def load(self, path: pathlib.Path) -> _ConfigData:
        ...


class _DotenvLoader(_Loader):
    @property
    @typing.override
    def flat(self) -> bool:
        return True

    @typing.override
    def load(self, path: pathlib.Path) -> _ConfigData:
        if not dotenv.load_dotenv(path):
            raise RuntimeError(f"Failed loading environment file '{path.resolve()}'.")
        return dict(os.environ)


class _YamlLoader(_Loader):
    @typing.override
    def load(self, path: pathlib.Path) -> _ConfigData:
        import yaml

        with open(path) as file:
            data = typing.cast(object, yaml.safe_load(file))
        if not _is_string_dict(data):
            raise ValueError(f"YAML file must contain a mapping, got {type(data).__name__}.")
        return data


class _TomlLoader(_Loader):
    @typing.override
    def load(self, path: pathlib.Path) -> _ConfigData:
        import tomllib

        with open(path, "rb") as file:
            try:
                data = typing.cast(object, tomllib.load(file))
            except tomllib.TOMLDecodeError as exc:
                raise ValueError(f"TOML file could not be parsed: {exc}") from exc
        if not _is_string_dict(data):
            raise ValueError(f"TOML file must contain a mapping, got {type(data).__name__}.")
        return data


class _JsonLoader(_Loader):
    @typing.override
    def load(self, path: pathlib.Path) -> _ConfigData:
        with open(path) as file:
            data = _json_loads(file.read())
        if not _is_string_dict(data):
            raise ValueError(f"JSON file must contain an object, got {type(data).__name__}.")
        return data


class _PythonLoader(_Loader):
    @typing.override
    def load(self, path: pathlib.Path) -> _ConfigData:
        module = ast.parse(path.read_text(), filename=str(path))
        data: _ConfigData = {}

        for statement in module.body:
            if isinstance(statement, ast.Assign):
                value = self._literal(statement.value)
                for target in statement.targets:
                    if isinstance(target, ast.Name) and not target.id.startswith("_"):
                        data[target.id] = value
                    elif not isinstance(target, ast.Name):
                        raise ValueError("Python config assignments must target simple names.")
                continue

            if isinstance(statement, ast.AnnAssign):
                if statement.value is None:
                    continue
                if not isinstance(statement.target, ast.Name):
                    raise ValueError("Python config assignments must target simple names.")
                if not statement.target.id.startswith("_"):
                    data[statement.target.id] = self._literal(statement.value)
                continue

            if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str):
                continue

            raise ValueError("Python config files may only contain literal assignments.")

        return data

    @staticmethod
    def _literal(node: ast.AST) -> object:
        try:
            value = typing.cast(object, ast.literal_eval(node))
        except (ValueError, TypeError) as exc:
            raise ValueError("Python config values must be literals.") from exc
        if isinstance(value, dict):
            mapping = typing.cast(object, value)
            if not _is_string_dict(mapping):
                raise ValueError("Python config dictionaries must use string keys.")
            return mapping
        return value


_LOADERS: dict[str, _Loader] = {
    ".env": _DotenvLoader(),
    ".yaml": _YamlLoader(),
    ".yml": _YamlLoader(),
    ".json": _JsonLoader(),
    ".toml": _TomlLoader(),
    ".py": _PythonLoader(),
}


def _is_base_model_type(annotation: object) -> typing.TypeGuard[type[BaseModel]]:
    return isinstance(annotation, type) and issubclass(annotation, BaseModel)


def _annotation_allows_none(annotation: object) -> bool:
    if annotation is None or annotation is type(None):
        return True

    origin = typing.cast(object, typing.get_origin(annotation))
    if origin is types.UnionType or str(origin) == "typing.Union":
        return any(_annotation_allows_none(arg) for arg in typing.cast(tuple[object, ...], typing.get_args(annotation)))

    return False


def _list_item_model(annotation: object) -> type[BaseModel] | None:
    origin = typing.cast(object, typing.get_origin(annotation))
    if origin is not list and origin is not Sequence:
        return None

    args = typing.cast(tuple[object, ...], typing.get_args(annotation))
    if len(args) != 1:
        return None

    item_type = args[0]
    return item_type if _is_base_model_type(item_type) else None


def _decode_flat_value(value: object) -> object:
    if not isinstance(value, str):
        return value

    stripped = value.strip()
    if stripped.startswith(("{", "[")):
        try:
            return _json_loads(stripped)
        except json.JSONDecodeError:
            return value

    return value


def _remap_value_for_model(value: object, annotation: object) -> object:
    if _is_base_model_type(annotation) and _is_string_dict(value):
        return _model_input_from_mapping(value, annotation)

    item_model = _list_item_model(annotation)
    if item_model is not None and isinstance(value, list):
        return [
            _model_input_from_mapping(item, item_model) if _is_string_dict(item) else item
            for item in typing.cast(list[object], value)
        ]

    return value


def _mapping_value_for_field(data: _ConfigData, field_name: str) -> tuple[bool, object]:
    key = _normalise_key(field_name)
    for candidate, value in data.items():
        if _normalise_key(candidate) == key:
            return True, value
    return False, None


def _model_input_from_mapping(data: _ConfigData, model: type[BaseModel]) -> _ConfigData:
    model_input: _ConfigData = {}

    for field_name, field_info in model.model_fields.items():
        annotation = typing.cast(object, field_info.annotation)
        exists, value = _mapping_value_for_field(data, field_name)
        if exists:
            model_input[field_name] = _remap_value_for_model(value, annotation)
        elif _annotation_allows_none(annotation):
            model_input[field_name] = None

    return model_input


def _flat_model_input(
    data: _ConfigData,
    model: type[BaseModel],
    *,
    prefix: str = "",
) -> _ConfigData:
    model_input: _ConfigData = {}

    for field_name, field_info in model.model_fields.items():
        key = _normalise_key(field_name)
        full_key = f"{prefix}_{key}" if prefix else key
        annotation = typing.cast(object, field_info.annotation)

        if full_key in data:
            value = _decode_flat_value(data[full_key])
            model_input[field_name] = _remap_value_for_model(value, annotation)
            continue

        if _is_base_model_type(annotation):
            prefix_probe = full_key + "_"
            if any(key.startswith(prefix_probe) for key in data):
                model_input[field_name] = _flat_model_input(data, annotation, prefix=full_key)
                continue

        if _annotation_allows_none(annotation):
            model_input[field_name] = None

    return model_input


class _Scope:
    def __init__(
        self,
        data: _ConfigData,
        *,
        flat: bool = False,
        prefix: str = "",
    ) -> None:
        self._data: _ConfigData = data
        self._flat: bool = flat
        self._prefix: str = prefix
        self._cache: dict[str, ConfValue] = {}

    def _resolve(self, name: str) -> ConfValue:
        key = _normalise_key(name)

        if self._flat:
            full_key = f"{self._prefix}_{key}" if self._prefix else key
            value = self._data.get(full_key)
            if value is not None:
                return ConfValue(_normalise_value(value))

            prefix_probe = full_key + "_"
            if any(key.startswith(prefix_probe) for key in self._data):
                return typing.cast(ConfValue, typing.cast(object, _Scope(self._data, flat=True, prefix=full_key)))

            raise EnvironmentError(f"Missing environment variable '{full_key}'.")

        value = self._data.get(key)
        if value is None:
            label = f" in scope '{self._prefix}'" if self._prefix else ""
            raise EnvironmentError(f"Missing configuration key '{key}'{label}.")

        if isinstance(value, dict):
            child_prefix = f"{self._prefix}.{key}" if self._prefix else key
            return typing.cast(
                ConfValue,
                typing.cast(object, _Scope(typing.cast(_ConfigData, value), prefix=child_prefix)),
            )

        return ConfValue(_normalise_value(value))

    def __getattr__(self, name: str, /) -> ConfValue:
        if name.startswith("_"):
            raise AttributeError(f"Attempted to access private member '{name}'.")

        if name not in self._cache:
            self._cache[name] = self._resolve(name)
        return self._cache[name]

    @typing.override
    def __repr__(self) -> str:
        kind = "flat" if self._flat else "nested"
        prefix = self._prefix or "(root)"
        return f"<_Scope {kind} prefix={prefix!r}>"


class _NullScopeMeta(type):
    def __getattr__(cls, name: str) -> "_NullScopeMeta":
        if name.startswith("_"):
            raise AttributeError(name)
        return cls

    def __bool__(cls) -> bool:
        return False

    @typing.override
    def __eq__(cls, other: object) -> bool:
        return other is None or other is cls

    @typing.override
    def __hash__(cls) -> int:
        return hash(None)

    @typing.override
    def __repr__(cls) -> str:
        return "NullScope"

    @typing.override
    def __str__(cls) -> str:
        return ""

    def one_of(cls, *_values: str | None) -> None:
        return None

    def __and__(cls, _values: Sequence[str | None]) -> None:
        return None


class _NullScope(metaclass=_NullScopeMeta):
    pass


_MaybeScopeResult: typing.TypeAlias = "ConfValue | type[_NullScope]"


class _MaybeScope:
    def __init__(
        self,
        data: _ConfigData,
        *,
        flat: bool = False,
        prefix: str = "",
    ) -> None:
        self._data: _ConfigData = data
        self._flat: bool = flat
        self._prefix: str = prefix
        self._cache: dict[str, _MaybeScopeResult] = {}
        self._resolved: set[str] = set()

    def _resolve(self, name: str) -> _MaybeScopeResult:
        key = _normalise_key(name)

        if self._flat:
            full_key = f"{self._prefix}_{key}" if self._prefix else key
            value = self._data.get(full_key)
            if value is not None:
                return ConfValue(_normalise_value(value))

            prefix_probe = full_key + "_"
            if any(key.startswith(prefix_probe) for key in self._data):
                return typing.cast(
                    ConfValue,
                    typing.cast(object, _MaybeScope(self._data, flat=True, prefix=full_key)),
                )

            return _NullScope

        value = self._data.get(key)
        if value is None:
            return _NullScope

        if isinstance(value, dict):
            child_prefix = f"{self._prefix}.{key}" if self._prefix else key
            return typing.cast(
                ConfValue,
                typing.cast(object, _MaybeScope(typing.cast(_ConfigData, value), prefix=child_prefix)),
            )

        return ConfValue(_normalise_value(value))

    def __getattr__(self, name: str, /) -> _MaybeScopeResult:
        if name.startswith("_"):
            raise AttributeError(f"Attempted to access private member '{name}'.")

        if name not in self._resolved:
            self._cache[name] = self._resolve(name)
            self._resolved.add(name)

        return self._cache[name]

    @typing.override
    def __repr__(self) -> str:
        kind = "flat" if self._flat else "nested"
        prefix = self._prefix or "(root)"
        return f"<_MaybeScope {kind} prefix={prefix!r}>"


class Config(_Scope):
    """Load configuration from a file and expose values through attributes."""

    def __init__(self, path: str | pathlib.Path = ".env") -> None:
        self.path: pathlib.Path = pathlib.Path(path)

        if not self.path.is_file():
            raise FileNotFoundError(f"Configuration file '{self.path.resolve()}' not found.")

        loader = _LOADERS.get(self.path.suffix or self.path.name)
        if loader is None:
            raise ValueError(f"Unsupported file type for configuration: '{self.path.suffix}'.")

        data = loader.load(self.path)
        self._model_data: _ConfigData = data
        scope_data = data if loader.flat else typing.cast(_ConfigData, _normalise_data(data))
        super().__init__(scope_data, flat=loader.flat)
        self._maybe: _MaybeScope | None = None

    @property
    def maybe(self) -> _MaybeScope:
        if self._maybe is None:
            self._maybe = _MaybeScope(self._data, flat=self._flat)
        return self._maybe

    def load[_ModelT: BaseModel](self, model: type[_ModelT]) -> _ModelT:
        if not _is_base_model_type(model):
            raise TypeError("Config.load() expects a pydantic.BaseModel subclass.")

        data = (
            _flat_model_input(self._model_data, model)
            if self._flat
            else _model_input_from_mapping(self._model_data, model)
        )
        return model.model_validate(data)

    @typing.override
    def __repr__(self) -> str:
        return f"<Config path={self.path!r}>"


class _LazyConfig:
    _config: Config | None

    def __init__(self) -> None:
        self._config = None

    def _get(self) -> Config:
        if self._config is None:
            self._config = Config()
        return self._config

    def __getattr__(self, name: str) -> object:
        return typing.cast(object, getattr(self._get(), name))

    @typing.override
    def __repr__(self) -> str:
        if self._config is None:
            return "<LazyConfig path='.env'>"
        return repr(self._config)


environ = typing.cast(Config, typing.cast(object, _LazyConfig()))
