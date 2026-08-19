"""mootdx 可用性探针 —— 同 announcement_risk 的 pypdf 修复：可选依赖缺失
必须可被调用方识别，不能和「上游没数据」混为一谈。

mootdx 只在可选 extra ``deephistory`` 里，CI 与默认生产安装都没有它，所以这里
不断言 ``mootdx_available() is True``，只断言探针与真实 import 结果一致、
且不可用时会短路（不去连 TCP 7709）。
"""

import mootdx_adapter


def _really_importable() -> bool:
    try:
        from mootdx.quotes import Quotes  # noqa: F401
    except ImportError:
        return False
    return True


def test_probe_matches_real_import():
    mootdx_adapter.mootdx_available.cache_clear()
    assert mootdx_adapter.mootdx_available() is _really_importable()


def test_missing_package_short_circuits_client(monkeypatch):
    monkeypatch.setattr(mootdx_adapter, "_MOOTDX_CLIENT", None)
    monkeypatch.setattr(mootdx_adapter, "mootdx_available", lambda: False)

    assert mootdx_adapter._get_client() is None
    # 短路发生在建连之前：锁没有被置位，说明没走到 Quotes.factory()
    assert mootdx_adapter._MOOTDX_CLIENT_LOCK is False


def test_client_factory_uses_best_server_and_bounded_timeout(monkeypatch):
    import sys
    import types

    calls = {}

    class FakeClient:
        def quotes(self, **kwargs):
            return {"code": kwargs.get("symbol", "000001")}

    class FakeQuotes:
        @staticmethod
        def factory(**kwargs):
            calls.update(kwargs)
            return FakeClient()

    fake_module = types.ModuleType("mootdx.quotes")
    fake_module.Quotes = FakeQuotes
    monkeypatch.setitem(sys.modules, "mootdx.quotes", fake_module)
    monkeypatch.setattr(mootdx_adapter, "mootdx_available", lambda: True)
    monkeypatch.setattr(mootdx_adapter, "_MOOTDX_CLIENT", None)

    assert mootdx_adapter._get_client() is not None
    assert calls["bestip"] is True
    assert calls["timeout"] > 0
    assert calls["market"] == "std"


def test_failed_client_probe_is_cached_for_process(monkeypatch):
    import sys
    import types

    calls = {"factory": 0}

    class FakeQuotes:
        @staticmethod
        def factory(**kwargs):
            calls["factory"] += 1
            raise RuntimeError("simulated TCP failure")

    fake_module = types.ModuleType("mootdx.quotes")
    fake_module.Quotes = FakeQuotes
    monkeypatch.setitem(sys.modules, "mootdx.quotes", fake_module)
    monkeypatch.setattr(mootdx_adapter, "mootdx_available", lambda: True)
    monkeypatch.setattr(mootdx_adapter, "_MOOTDX_CLIENT", None)

    assert mootdx_adapter._get_client() is None
    assert mootdx_adapter._get_client() is None
    assert calls["factory"] == 1


def test_missing_package_is_logged(monkeypatch, caplog):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("mootdx"):
            raise ImportError("simulated missing mootdx")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    mootdx_adapter.mootdx_available.cache_clear()
    try:
        with caplog.at_level("WARNING", logger=mootdx_adapter.__name__):
            assert mootdx_adapter.mootdx_available() is False
    finally:
        monkeypatch.undo()
        mootdx_adapter.mootdx_available.cache_clear()

    assert "mootdx 未安装" in caplog.text
