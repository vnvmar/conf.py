# conf

`conf` is a small Python configuration loader. It reads configuration from
`*.env`, JSON, YAML, TOML, and safe literal Python files, then exposes values
through cached attribute access.

The casing to access these fields does not matter, as they are normalised internally.

```python
from conf import Config

config = Config("config.yaml")

token = config.API.TOKEN # or config.api.token
debug = config.DEBUG.to(bool)
ports = config.PORTS.to(list)
```

Flat `.env` files support scoped access by joining segments with underscores:

```dotenv
SERVICE_DATABASE_URL=postgres://localhost/app
```

```python
config = Config(".env")
config.SERVICE.DATABASE.URL 
```

Structured files use nested mappings directly:

```yaml
service:
  database:
    url: postgres://localhost/app
```

Missing keys normally raise `EnvironmentError`. Use `maybe` when optional
configuration should stay falsy instead:

```python
timeout = config.maybe.TIMEOUT
if not timeout:
    timeout = "30"
```

## Values

Lookups return `ConfValue`, a `str` subclass with parsing helpers:

```python
config.PORT.to(int)
config.RATIO.to(float)
config.DEBUG.to(bool)
config.TAGS.to(list)
config.PAYLOAD.to(dict)
config.LOG_LEVEL.one_of("debug", "info", "warning", "error")
# or
config.LOG_LEVEL & ("debug", "info", "warning", "error")
```

## Pydantic

One problem with all the above options, is that it is untyped. If type safety is desired, one can create a `pydantic.BaseModel`
class and load it directly.

`Config.load()` can validate loaded configuration into a Pydantic model, even nested configurations:

```python
from pydantic import BaseModel
from conf import Config


class Database(BaseModel):
    host: str
    port: int


class Settings(BaseModel):
    debug: bool
    database: Database
    token: str | None


settings = Config("config.toml").load(Settings)
```

## Python Config Files

Python config files are parsed with `ast.literal_eval`; they are not executed.
Only top-level literal assignments are allowed:

```python
DEBUG = True
PORT = 8000
API = {"token": "secret"}
```

Imports, function calls, and other executable code raise `ValueError`.

## Development

Run the tests with:

```bash
uv run pytest
```
