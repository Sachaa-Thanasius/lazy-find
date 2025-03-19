# A substantial portion of the code and comments below is adapted from
# https://github.com/python/cpython/blob/49234c065cf2b1ea32c5a3976d834b1d07b9b831/Lib/importlib/util.py and
# https://github.com/python/cpython/blob/49234c065cf2b1ea32c5a3976d834b1d07b9b831/Lib/importlib/_bootstrap.py
# with the original copyright being:
# Copyright (c) 2001 Python Software Foundation; All Rights Reserved
#
# The license in its original form may be found at
# https://github.com/python/cpython/blob/49234c065cf2b1ea32c5a3976d834b1d07b9b831/LICENSE
# and in this repository at ``LICENSE_cpython``.

from __future__ import annotations

import _imp
import _thread
import sys as _sys
import warnings as _warnings
from importlib.machinery import ModuleSpec as _ModuleSpec, SourceFileLoader as _SourceFileLoader


TYPE_CHECKING = False


# importlib.abc.Loader changed location in 3.10+ to become cheaper to import.
if TYPE_CHECKING:
    from importlib.abc import Loader as _Loader
else:
    try:  # pragma: >=3.10 cover
        from importlib._abc import Loader as _Loader
    except ImportError:  # pragma: <3.10 cover
        from importlib.abc import Loader as _Loader


# types provides more than we need but is better understood by type-checkers.
if TYPE_CHECKING:
    from types import ModuleType as _ModuleType
else:
    _ModuleType = type(_sys)


__all__ = ("lazy_finder",)


# ============================================================================
# region -------- Adapted from importlib._bootstrap
#
# This avoids importing from a module that's an implementation detail.
# `_imp` ends up being needed, but it's arguably less problematic.
# ============================================================================


class _ImportLockContext:
    """Context manager for the import lock."""

    def __enter__(self, /) -> None:
        """Acquire the import lock."""

        _imp.acquire_lock()

    def __exit__(self, *_dont_care: object) -> None:
        """Release the import lock regardless of any raised exceptions."""

        _imp.release_lock()


def _find_spec(
    name: str,
    path: _t.Optional[_t.Sequence[str]],
    target: _t.Optional[_ModuleType] = None,
) -> _t.Optional[_ModuleSpec]:  # pragma: no cover
    """Find a module's spec."""

    meta_path = _sys.meta_path
    if meta_path is None:  # pyright: ignore [reportUnnecessaryComparison]
        msg = "sys.meta_path is None, Python is likely shutting down"
        raise ImportError(msg)

    # gh-130094: Copy sys.meta_path so that we have a consistent view of the
    # list while iterating over it.
    meta_path = list(meta_path)
    if not meta_path:
        _warnings.warn("sys.meta_path is empty", ImportWarning)  # noqa: B028

    # We check sys.modules here for the reload case.  While a passed-in
    # target will usually indicate a reload there is no guarantee, whereas
    # sys.modules provides one.
    is_reload = name in _sys.modules
    for finder in meta_path:
        with _ImportLockContext():
            try:
                find_spec = finder.find_spec
            except AttributeError:
                continue
            else:
                spec = find_spec(name, path, target)

        if spec is not None:
            # The parent import may have already imported this module.
            if not is_reload and name in _sys.modules:
                module = _sys.modules[name]
                try:
                    __spec__ = module.__spec__
                except AttributeError:
                    # We use the found spec since that is the one that
                    # we would have used if the parent module hadn't
                    # beaten us to the punch.
                    return spec
                else:
                    if __spec__ is None:
                        return spec
                    else:
                        return __spec__
            else:
                return spec

    return None


# endregion


# ============================================================================
# region -------- Adapted from importlib.util
#
# This allows a few invasive changes to the LazyLoader chain:
#    1. Replace `threading` import with `_thread` to match which module
#       importlib uses internally, and do it at the top level to avoid
#       circular import issues.
#    2. Special-casing `__spec__` in the lazy module type to avoid loading
#       being unnecessarily triggered by internal importlib machinery.
#    3. Avoiding importing `types`.
#    4. Slightly adjusting method signatures.
# ============================================================================


class _LazyModuleType(_ModuleType):
    """A subclass of the module type which triggers loading upon attribute access."""

    def __getattribute__(self, name: str, /) -> _t.Any:
        """Trigger the load of the module and return the attribute."""

        __spec__: _ModuleSpec = object.__getattribute__(self, "__spec__")

        # We want to avoid the importlib machinery unnecessarily causing a load
        # when it checks a lazy module in sys.modules to see if it is initialized.
        # Since the machinery determines that via module.__spec__, return that without loading.
        #
        # This does mean a user can get __spec__ from a lazy module and modify it without causing a load. However,
        # for our use case, this should be good enough.
        if name == "__spec__":
            return __spec__

        loader_state = __spec__.loader_state
        with loader_state["lock"]:
            # Only the first thread to get the lock should trigger the load
            # and reset the module's class. The rest can now getattr().
            if object.__getattribute__(self, "__class__") is _LazyModuleType:
                __class__ = loader_state["__class__"]

                # Reentrant calls from the same thread must be allowed to proceed without
                # triggering the load again.
                # exec_module() and self-referential imports are the primary ways this can
                # happen, but in any case we must return something to avoid deadlock.
                if loader_state["is_loading"]:
                    return __class__.__getattribute__(self, name)
                loader_state["is_loading"] = True

                __dict__: dict[str, _t.Any] = __class__.__getattribute__(self, "__dict__")

                # All module metadata must be gathered from __spec__ in order to avoid
                # using mutated values.
                # Get the original name to make sure no object substitution occurred
                # in sys.modules.
                original_name = __spec__.name

                # Figure out exactly what attributes were mutated between the creation
                # of the module and now.
                attrs_then: dict[str, _t.Any] = loader_state["__dict__"]
                attrs_now = __dict__
                attrs_updated = {
                    key: value
                    for key, value in attrs_now.items()
                    # Code that set an attribute may have kept a reference to the
                    # assigned object, making identity more important than equality.
                    if (key not in attrs_then) or (attrs_now[key] is not attrs_then[key])
                }

                assert __spec__.loader is not None, "This spec must have an actual loader."
                __spec__.loader.exec_module(self)

                # If exec_module() was used directly there is no guarantee the module
                # object was put into sys.modules.
                if (original_mod := _sys.modules.get(original_name, None)) is not None and (self is not original_mod):
                    msg = f"module object for {original_name!r} substituted in sys.modules during a lazy load"
                    raise ValueError(msg)

                # Update after loading since that's what would happen in an eager
                # loading situation.
                __dict__ |= attrs_updated

                # Finally, stop triggering this method, if the module did not
                # already update its own __class__.
                if isinstance(self, _LazyModuleType):
                    object.__setattr__(self, "__class__", __class__)

        return getattr(self, name)

    def __delattr__(self, name: str, /) -> None:
        """Trigger the load and then perform the deletion."""

        # To trigger the load and raise an exception if the attribute
        # doesn't exist.
        self.__getattribute__(name)
        delattr(self, name)


class _LazyLoader(_Loader):
    """A loader that creates a module which defers loading until attribute access."""

    @staticmethod
    def __check_eager_loader(loader: _Loader) -> None:
        if not hasattr(loader, "exec_module"):
            msg = "loader must define exec_module()"
            raise TypeError(msg)

    def __init__(self, loader: _Loader) -> None:
        self.__check_eager_loader(loader)
        self.loader = loader

    def create_module(self, spec: _ModuleSpec) -> _t.Optional[_ModuleType]:
        return self.loader.create_module(spec)

    def exec_module(self, module: _ModuleType) -> None:
        """Make the module load lazily."""

        assert module.__spec__ is not None, "The module should have been initialized with a spec."

        module.__spec__.loader = self.loader
        module.__loader__ = self.loader

        # Don't need to worry about deep-copying as trying to set an attribute
        # on an object would have triggered the load,
        # e.g. ``module.__spec__.loader = None`` would trigger a load from
        # trying to access module.__spec__.
        loader_state = {
            "__dict__": module.__dict__.copy(),
            "__class__": module.__class__,
            "lock": _thread.RLock(),
            "is_loading": False,
        }
        module.__spec__.loader_state = loader_state
        module.__class__ = _LazyModuleType


# endregion


# ============================================================================
# region -------- Original
# ============================================================================


# XXX: Should we use a global thread rlock to guard our modifications of sys.meta_path?
# It won't help if others modify the meta path, but it'll prevent our code from data racing
# itself. In theory.


class _LazyFinder:
    """A finder that wraps loaders for source modules with `_LazyLoader`."""

    @classmethod
    def find_spec(
        cls,
        name: str,
        path: _t.Optional[_t.Sequence[str]] = None,
        target: _t.Optional[_ModuleType] = None,
    ) -> _t.Optional[_ModuleSpec]:
        in_meta_path = cls in _sys.meta_path

        if in_meta_path:
            _sys.meta_path.remove(cls)

        try:
            spec = _find_spec(name, path, target)

            # Skip being lazy for non-source modules to avoid issues with extension modules having
            # uninitialized state, especially when loading can't currently be triggered by PyModule_GetState.
            # Ref: https://github.com/python/cpython/issues/85963
            if (spec is not None) and isinstance(spec.loader, _SourceFileLoader):
                spec.loader = _LazyLoader(spec.loader)

            return spec

        finally:
            if in_meta_path:
                _sys.meta_path.insert(0, cls)


class _LazyFinderContext:
    """A context manager that temporarily lazifies contained imports (if the modules are written in Python)."""

    __slots__ = ()

    def __enter__(self, /) -> None:
        _sys.meta_path.insert(0, _LazyFinder)

    def __exit__(self, *_dont_care: object) -> None:
        try:
            _sys.meta_path.remove(_LazyFinder)
        except ValueError:
            _warnings.warn("_LazyFinder unexpectedly missing from sys.meta_path", ImportWarning, stacklevel=2)


lazy_finder: _t.Final[_LazyFinderContext] = _LazyFinderContext()


# Support our type annotations valid at runtime.
with lazy_finder:
    import typing as _t


# endregion
