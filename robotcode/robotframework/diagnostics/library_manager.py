import ast
import asyncio
import os
from collections import OrderedDict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Optional, Sequence, Tuple
from multiprocessing import Pool

DEFAULT_LIBRARIES = ("BuiltIn", "Reserved", "Easter")


@dataclass
class KeywordDoc:
    name: str = ""
    args: Tuple[Any, ...] = ()
    doc: str = ""
    tags: Tuple[str, ...] = ()
    source: Optional[str] = None
    lineno: int = -1

    def __str__(self) -> str:
        return f"{self.name}({', '.join(str(arg) for arg in self.args)})"


@dataclass
class LibraryDoc:
    name: str = ""
    doc: str = ""
    version: str = ""
    type: str = "LIBRARY"
    scope: str = "TEST"
    named_args: bool = True
    doc_format: str = "ROBOT"
    source: Optional[str] = None
    lineno: int = -1
    inits: Sequence[KeywordDoc] = ()
    keywords: Sequence[KeywordDoc] = ()

    def __str__(self) -> str:
        return self.name


def _is_library_by_path(path: str) -> bool:
    return path.lower().endswith((".py", ".java", ".class", "/", os.sep))


def _get_library_doc(
    name: str, args: Optional[Tuple[Any, ...]] = None, working_dir: str = ".", base_dir: str = "."
) -> LibraryDoc:
    import sys
    from pathlib import Path

    from robot.libdocpkg.robotbuilder import KeywordDocBuilder
    from robot.running.testlibraries import TestLibrary
    from robot.utils.robotpath import find_file

    # correct sys path
    file = Path(__file__).resolve()
    top = file.parents[3]

    for p in filter(lambda v: Path(v).is_relative_to(top), sys.path.copy()):
        sys.path.remove(p)

    wd = Path(working_dir)
    os.chdir(wd)

    if _is_library_by_path(name):
        name = find_file(name, base_dir or ".", "Library")

    lib = TestLibrary(name, args)
    libdoc = LibraryDoc(
        name=str(lib.name),
        doc=str(lib.doc),
        version=str(lib.version),
        scope=str(lib.scope),
        doc_format=str(lib.doc_format),
        source=lib.source,
        lineno=lib.lineno,
    )
    libdoc.inits = [
        KeywordDoc(
            name=kw.name, args=tuple(kw.args), doc=kw.doc, tags=tuple(kw.tags), source=kw.source, lineno=kw.lineno
        )
        for kw in [KeywordDocBuilder().build_keyword(lib.init)]
    ]
    libdoc.keywords = [
        KeywordDoc(
            name=kw.name, args=tuple(kw.args), doc=kw.doc, tags=tuple(kw.tags), source=kw.source, lineno=kw.lineno
        )
        for kw in KeywordDocBuilder().build_keywords(lib)
    ]

    return libdoc


@dataclass
class _LibraryEntry:
    name: str
    args: Tuple[Any, ...]

    def __hash__(self) -> int:
        return hash((self.name, self.args))


class LibraryManager:
    def __init__(self) -> None:
        super().__init__()
        self.process_pool = ProcessPoolExecutor()
        self._libaries_lock = asyncio.Lock()
        self._libaries: OrderedDict[_LibraryEntry, LibraryDoc] = OrderedDict()
        self.loop = asyncio.get_event_loop()
        self.pool = Pool()

    def __del__(self) -> None:
        self.process_pool.shutdown(False)
        self.pool.close()

    async def get_doc_from_library(self, name: str, args: Tuple[Any, ...] = (), base_dir: str = ".") -> LibraryDoc:
        entry = _LibraryEntry(name, args)

        async def get() -> LibraryDoc:
            return await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(
                    self.process_pool, _get_library_doc, name, args, ".", base_dir
                ),
                10,
            )

        async with self._libaries_lock:
            if entry not in self._libaries:
                # self._libaries[entry] = await get()
                self._libaries[entry] = self.pool.apply_async(_get_library_doc, args=(name, args, ".", base_dir)).get(
                    10
                )

            return self._libaries[entry]

    async def get_doc_from_model(
        self, model: ast.AST, source: str, type: str = "RESOURCE", scope: str = "GLOBAL"
    ) -> LibraryDoc:

        from robot.libdocpkg.robotbuilder import KeywordDocBuilder
        from robot.running.builder.transformers import ResourceBuilder
        from robot.running.model import ResourceFile
        from robot.running.userkeyword import UserLibrary

        res = ResourceFile(source=source)

        ResourceBuilder(res).visit(model)

        lib = UserLibrary(res)

        libdoc = LibraryDoc(name=lib.name or "", doc=lib.doc, type=type, scope=scope, source=source, lineno=1)

        libdoc.keywords = [
            KeywordDoc(
                name=kw.name, args=tuple(kw.args), doc=kw.doc, tags=tuple(kw.tags), source=kw.source, lineno=kw.lineno
            )
            for kw in KeywordDocBuilder().build_keywords(lib)
        ]

        libdoc.keywords = [
            KeywordDoc(
                name=kw.name, args=tuple(kw.args), doc=kw.doc, tags=tuple(kw.tags), source=kw.source, lineno=kw.lineno
            )
            for kw in KeywordDocBuilder(resource=type == "RESOURCE").build_keywords(lib)
        ]

        return libdoc
