
"""Minimal pytest stub for offline validation."""
import functools

class _Mark:
    def parametrize(self, *a, **kw):
        def dec(fn):
            fn._params = a
            return fn
        return dec
    def __getattr__(self, name):
        def dec(*a, **kw):
            def wrap(fn): return fn
            return wrap
        return dec

mark = _Mark()

class fixture:
    def __init__(self, fn=None, scope="function", autouse=False):
        if fn: functools.wraps(fn)(self); self._fn = fn
    def __call__(self, *a, **kw):
        return self._fn(*a, **kw) if hasattr(self,"_fn") else None
    def __get__(self, obj, cls): return self

class raises:
    def __init__(self, exc): self.exc = exc
    def __enter__(self): return self
    def __exit__(self, et, ev, tb): return et is not None and issubclass(et, self.exc)

def skip(reason=""): pass
def xfail(fn): return fn
def approx(v, rel=None, abs=None): return v
