"""测试专用依赖垫片：本机未安装 fastapi/sqlalchemy/redis 时注入最小替身。

生产环境安装 requirements.txt 后这些垫片完全不会生效（真实模块已存在则跳过）。
"""
import sys
import types


class _HTTPException(Exception):
    def __init__(self, status_code=400, detail=""):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class _Column:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self._name = kwargs.get("_name", "col")

    def __eq__(self, other):
        from tests.fake_orm import fake_where
        return fake_where(self._name, other)

    def __hash__(self):
        return id(self)


def _already_real(name):
    return name in sys.modules and not getattr(sys.modules[name], "_is_shim", False)


def install():
    if not _already_real("fastapi"):
        fastapi = types.ModuleType("fastapi")
        fastapi._is_shim = True
        fastapi.HTTPException = _HTTPException

        class _APIRouter:
            def __init__(self, *a, **k):
                pass

            def get(self, *a, **k):
                return lambda f: f

            post = put = delete = get

        fastapi.APIRouter = _APIRouter
        fastapi.Depends = lambda *a, **k: None
        sys.modules["fastapi"] = fastapi

    if not _already_real("sqlalchemy"):
        from tests.fake_orm import FakeSelect
        sqlalchemy = types.ModuleType("sqlalchemy")
        sqlalchemy._is_shim = True
        sqlalchemy.Column = _Column

        class _Type:
            def __init__(self, *a, **k):
                self.args = a

            def __call__(self, *a, **k):
                return _Type(*a, **k)

        sqlalchemy.Integer = sqlalchemy.String = sqlalchemy.JSON = _Type()
        sqlalchemy.DateTime = sqlalchemy.Text = _Type()
        sqlalchemy.ForeignKey = lambda *a, **k: ("fk", a)
        sqlalchemy.select = lambda model: FakeSelect(model)
        sqlalchemy.func = types.SimpleNamespace(count=lambda: "count")
        exc = types.ModuleType("sqlalchemy.exc")
        exc.IntegrityError = type("IntegrityError", (Exception,), {})
        sqlalchemy.exc = exc
        orm = types.ModuleType("sqlalchemy.orm")

        class _DeclarativeBase:
            def __init__(self, **kw):
                self._model = type(self)
                for key, value in kw.items():
                    setattr(self, key, value)

            @property
            def __table__(self):
                class _Col:
                    def __init__(self, name):
                        self.name = name

                class _Table:
                    def __init__(self, owner):
                        self.columns = [_Col(k) for k in vars(owner)
                                        if not k.startswith("_") and k != "__table__"]

                return _Table(self)

        orm.DeclarativeBase = _DeclarativeBase
        orm.sessionmaker = lambda *a, **k: None
        sys.modules["sqlalchemy"] = sqlalchemy
        sys.modules["sqlalchemy.exc"] = exc
        sys.modules["sqlalchemy.orm"] = orm

    if "src.config.database" not in sys.modules:
        config_db = types.ModuleType("src.config.database")
        config_db.Base = sys.modules["sqlalchemy.orm"].DeclarativeBase
        config_db.engine = None
        config_db.Session = None
        config_db.db_session = lambda: None
        sys.modules["src.config.database"] = config_db

    if "src.config.settings" not in sys.modules:
        settings = types.ModuleType("src.config.settings")
        settings.DATABASE_URL = "sqlite:///:memory:"
        settings.REDIS_URL = "redis://localhost"
        settings.JWT_SECRET = "test"
        sys.modules["src.config.settings"] = settings

    if not _already_real("redis"):
        redis_mod = types.ModuleType("redis")
        redis_mod._is_shim = True

        class _Redis:
            @classmethod
            def from_url(cls, *a, **k):
                return cls()

        redis_mod.Redis = _Redis
        sys.modules["redis"] = redis_mod
