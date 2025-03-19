# A substantial portion of the code and comments below is adapted from
# https://github.com/python/cpython/blob/49234c065cf2b1ea32c5a3976d834b1d07b9b831/Lib/test/test_importlib/test_lazy.py
# with the original copyright being:
# Copyright (c) 2001 Python Software Foundation; All Rights Reserved
#
# The license in its original form may be found at
# https://github.com/python/cpython/blob/49234c065cf2b1ea32c5a3976d834b1d07b9b831/LICENSE
# and in this repository at ``LICENSE_cpython``.

import importlib.abc
import importlib.util
import sys
import threading
import time
import types
import unittest

from test.test_importlib import util as test_util

from lazy_finder import _LazyFinder, _LazyLoader, _LazyModuleType, lazy_finder


if sys.version_info >= (3, 11):  # pragma: >=3.11 cover
    from test.support.threading_helper import requires_working_threading
else:  # pragma: <3.11 cover

    def _can_start_thread() -> bool:
        """Detect whether Python can start new threads.

        Some WebAssembly platforms do not provide a working pthread
        implementation. Thread support is stubbed and any attempt
        to create a new thread fails.

        - wasm32-wasi does not have threading.
        - wasm32-emscripten can be compiled with or without pthread
        support (-s USE_PTHREADS / __EMSCRIPTEN_PTHREADS__).
        """
        if sys.platform == "emscripten":
            return sys._emscripten_info.pthreads
        elif sys.platform == "wasi":
            return False
        else:
            # assume all other platforms have working thread support.
            return True

    can_start_thread = _can_start_thread()

    def requires_working_threading(*, module: bool = False):
        """Skip tests or modules that require working threading.

        Can be used as a function/class decorator or to skip an entire module.
        """
        msg = "requires threading support"
        if module:
            if not can_start_thread:
                raise unittest.SkipTest(msg)
            else:
                return None
        else:
            return unittest.skipUnless(can_start_thread, msg)


class TestingImporter(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    module_name = "lazy_loader_test"
    mutated_name = "changed"
    loaded = None
    load_count = 0
    source_code = f"attr = 42; __name__ = {mutated_name!r}"

    def find_spec(self, name, path, target=None):
        if name != self.module_name:  # pragma: no cover
            return None
        return importlib.util.spec_from_loader(name, _LazyLoader(self))

    def exec_module(self, module):
        time.sleep(0.01)  # Simulate a slow load.
        exec(self.source_code, module.__dict__)  # noqa: S102
        self.loaded = module
        self.load_count += 1


class LazyLoaderTests(unittest.TestCase):
    def test_init(self):
        with self.assertRaises(TypeError):
            # Classes that don't define exec_module() trigger TypeError.
            _LazyLoader(object)

    def new_module(self, source_code=None, loader=None):
        if loader is None:
            loader = TestingImporter()
        if source_code is not None:
            loader.source_code = source_code
        spec = importlib.util.spec_from_loader(TestingImporter.module_name, _LazyLoader(loader))
        module = spec.loader.create_module(spec)
        if module is None:  # pragma: no cover
            module = types.ModuleType(TestingImporter.module_name)
        module.__spec__ = spec
        module.__loader__ = spec.loader
        spec.loader.exec_module(module)
        # Module is now lazy.
        self.assertIsNone(loader.loaded)
        return module

    def test_e2e(self):
        # End-to-end test to verify the load is in fact lazy.
        importer = TestingImporter()
        assert importer.loaded is None
        with test_util.uncache(importer.module_name), test_util.import_state(meta_path=[importer]):
            module = importlib.import_module(importer.module_name)
        self.assertIsNone(importer.loaded)
        # Trigger load.
        self.assertEqual(module.__loader__, importer)
        self.assertIsNotNone(importer.loaded)
        self.assertEqual(module, importer.loaded)

    def test_attr_unchanged(self):
        # An attribute only mutated as a side-effect of import should not be
        # changed needlessly.
        module = self.new_module()
        self.assertEqual(TestingImporter.mutated_name, module.__name__)

    def test_new_attr(self):
        # A new attribute should persist.
        module = self.new_module()
        module.new_attr = 42
        self.assertEqual(42, module.new_attr)

    def test_mutated_preexisting_attr(self):
        # Changing an attribute that already existed on the module --
        # e.g. __name__ -- should persist.
        module = self.new_module()
        module.__name__ = "bogus"
        self.assertEqual("bogus", module.__name__)

    def test_mutated_attr(self):
        # Changing an attribute that comes into existence after an import
        # should persist.
        module = self.new_module()
        module.attr = 6
        self.assertEqual(6, module.attr)

    def test_delete_eventual_attr(self):
        # Deleting an attribute should stay deleted.
        module = self.new_module()
        del module.attr
        self.assertFalse(hasattr(module, "attr"))

    def test_delete_preexisting_attr(self):
        module = self.new_module()
        del module.__name__
        self.assertFalse(hasattr(module, "__name__"))

    def test_module_substitution_error(self):
        with test_util.uncache(TestingImporter.module_name):
            fresh_module = types.ModuleType(TestingImporter.module_name)
            sys.modules[TestingImporter.module_name] = fresh_module
            module = self.new_module()
            with self.assertRaisesRegex(ValueError, "substituted"):
                _ = module.__name__

    def test_module_already_in_sys(self):
        with test_util.uncache(TestingImporter.module_name):
            module = self.new_module()
            sys.modules[TestingImporter.module_name] = module
            # Force the load; just care that no exception is raised.
            _ = module.__name__

    @requires_working_threading()
    def test_module_load_race(self):
        with test_util.uncache(TestingImporter.module_name):
            loader = TestingImporter()
            module = self.new_module(loader=loader)
            self.assertEqual(loader.load_count, 0)

            class RaisingThread(threading.Thread):
                exc = None

                def run(self):
                    try:
                        super().run()
                    except Exception as exc:  # pragma: no cover # noqa: BLE001
                        self.exc = exc

            def access_module():
                return module.attr

            threads: list[RaisingThread] = []
            for _ in range(2):
                thread = RaisingThread(target=access_module)
                threads.append(thread)
                thread.start()

            # Races could cause errors
            for thread in threads:
                thread.join()
                self.assertIsNone(thread.exc)

            # Or multiple load attempts
            self.assertEqual(loader.load_count, 1)

    def test_lazy_self_referential_modules(self):
        # Directory modules with submodules that reference the parent can attempt to access
        # the parent module during a load. Verify that this common pattern works with lazy loading.
        # json is a good example in the stdlib.
        json_modules = [name for name in sys.modules if name.startswith("json")]
        with test_util.uncache(*json_modules):
            # Standard lazy loading, unwrapped
            spec = importlib.util.find_spec("json")
            loader = _LazyLoader(spec.loader)
            spec.loader = loader
            module = importlib.util.module_from_spec(spec)
            sys.modules["json"] = module
            loader.exec_module(module)

            # Trigger load with attribute lookup, ensure expected behavior
            test_load = module.loads("{}")
            self.assertEqual(test_load, {})

    def test_lazy_module_type_override(self):
        # Verify that lazy loading works with a module that modifies
        # its __class__ to be a custom type.

        # Example module from PEP 726
        module = self.new_module(
            source_code="""\
import sys
from types import ModuleType

CONSTANT = 3.14

class ImmutableModule(ModuleType):
    def __setattr__(self, name, value):
        raise AttributeError('Read-only attribute!')

    def __delattr__(self, name):
        raise AttributeError('Read-only attribute!')

sys.modules[__name__].__class__ = ImmutableModule
"""
        )
        sys.modules[TestingImporter.module_name] = module
        self.assertIsInstance(module, _LazyModuleType)
        self.assertEqual(module.CONSTANT, 3.14)
        with self.assertRaises(AttributeError):
            module.CONSTANT = 2.71
        with self.assertRaises(AttributeError):
            del module.CONSTANT

    def test_special_case___spec__(self):
        # Verify that getting/modifying module.__spec__ doesn't trigger the load.

        module = self.new_module()
        self.assertIs(object.__getattribute__(module, "__class__"), _LazyModuleType)
        _ = module.__spec__
        self.assertIs(object.__getattribute__(module, "__class__"), _LazyModuleType)
        module.__spec__.name = "blahblahblah"
        self.assertIs(object.__getattribute__(module, "__class__"), _LazyModuleType)


class LazyFinderTests(unittest.TestCase):
    def test_doesnt_wrap_non_source_file_loaders(self):
        for mod_type, mod_name in (("builtin", "sys"), ("frozen", "zipimport")):
            with self.subTest(mod_type, mod_name=mod_name):
                spec = _LazyFinder.find_spec(mod_name)
                self.assertIsNotNone(spec)
                self.assertNotIsInstance(spec.loader, _LazyLoader)

    def test_wraps_source_file_loader(self):
        spec = _LazyFinder.find_spec("inspect")
        self.assertIsNotNone(spec)
        self.assertIsInstance(spec.loader, _LazyLoader)

    def test_warning_if_missing_from_meta_path(self):
        with (
            test_util.uncache("inspect"),
            self.assertWarns(ImportWarning, msg="_LazyFinder unexpectedly missing from sys.meta_path"),
            lazy_finder,
        ):
            import inspect  # noqa: F401

            sys.meta_path.remove(_LazyFinder)

    def test_e2e(self):
        with test_util.uncache("typing"):
            # Lazily imported.
            with lazy_finder:
                import typing
            self.assertIs(object.__getattribute__(typing, "__class__"), _LazyModuleType)

            # When lazily imported again, still unloaded.
            with lazy_finder:
                import typing
            self.assertIs(object.__getattribute__(typing, "__class__"), _LazyModuleType)

            # When regularly imported but untouched, still unloaded.
            import typing

            self.assertIs(object.__getattribute__(typing, "__class__"), _LazyModuleType)

            # Only on accessing a variable is it loaded.
            _ = typing.Any

            self.assertIs(object.__getattribute__(typing, "__class__"), types.ModuleType)


if __name__ == "__main__":
    unittest.main()
