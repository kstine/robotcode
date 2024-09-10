import ast
from concurrent.futures import CancelledError
from typing import TYPE_CHECKING, Any, List, Optional

from robot.parsing.lexer.tokens import Token

from robotcode.core.concurrent import check_current_task_canceled
from robotcode.core.language import language_id
from robotcode.core.lsp.types import (
    Diagnostic,
    DiagnosticSeverity,
    DiagnosticTag,
    Position,
    Range,
)
from robotcode.core.text_document import TextDocument
from robotcode.core.uri import Uri
from robotcode.core.utils.logging import LoggingDescriptor
from robotcode.language_server.robotframework.configuration import AnalysisConfig
from robotcode.robot.diagnostics.entities import (
    ArgumentDefinition,
    EnvironmentVariableDefinition,
    GlobalVariableDefinition,
    LibraryArgumentDefinition,
)
from robotcode.robot.diagnostics.library_doc import LibraryDoc
from robotcode.robot.diagnostics.namespace import Namespace
from robotcode.robot.utils.ast import (
    iter_nodes,
    range_from_node,
    range_from_token,
)
from robotcode.robot.utils.stubs import HasError, HasErrors, HeaderAndBodyBlock

from ...common.parts.diagnostics import DiagnosticsCollectType, DiagnosticsResult

if TYPE_CHECKING:
    from ..protocol import RobotLanguageServerProtocol

from .protocol_part import RobotLanguageServerProtocolPart


class RobotDiagnosticsProtocolPart(RobotLanguageServerProtocolPart):
    _logger = LoggingDescriptor()

    def __init__(self, parent: "RobotLanguageServerProtocol") -> None:
        super().__init__(parent)

        self.source_name = "robotcode.diagnostics"

        self.parent.on_initialized.add(self._on_initialized)

        self.parent.diagnostics.collect.add(self.collect_token_errors)
        self.parent.diagnostics.collect.add(self.collect_model_errors)

        self.parent.diagnostics.collect.add(self.collect_namespace_diagnostics)

        self.parent.diagnostics.collect.add(self.collect_unused_keyword_references)
        self.parent.diagnostics.collect.add(self.collect_unused_variable_references)

        self.parent.diagnostics.on_get_related_documents.add(self._on_get_related_documents)

    def _on_initialized(self, sender: Any) -> None:
        self.parent.diagnostics.analyze.add(self.analyze)
        self.parent.documents_cache.namespace_invalidated.add(self._on_namespace_invalidated)
        self.parent.documents_cache.namespace_initialized(self._on_namespace_initialized)
        self.parent.documents_cache.libraries_changed.add(self._on_libraries_changed)
        self.parent.documents_cache.variables_changed.add(self._on_variables_changed)

    def _on_libraries_changed(self, sender: Any, libraries: List[LibraryDoc]) -> None:
        for doc in self.parent.documents.documents:
            namespace = self.parent.documents_cache.get_only_initialized_namespace(doc)
            if namespace is not None:
                lib_docs = (e.library_doc for e in namespace.get_libraries().values())
                if any(lib_doc in lib_docs for lib_doc in libraries):
                    self.parent.diagnostics.force_refresh_document(doc)

    def _on_variables_changed(self, sender: Any, variables: List[LibraryDoc]) -> None:
        for doc in self.parent.documents.documents:
            namespace = self.parent.documents_cache.get_only_initialized_namespace(doc)
            if namespace is not None:
                lib_docs = (e.library_doc for e in namespace.get_imported_variables().values())
                if any(lib_doc in lib_docs for lib_doc in variables):
                    self.parent.diagnostics.force_refresh_document(doc)

    @language_id("robotframework")
    def analyze(self, sender: Any, document: TextDocument) -> None:
        self.parent.documents_cache.get_namespace(document).analyze()

    @language_id("robotframework")
    def _on_namespace_initialized(self, sender: Any, namespace: Namespace) -> None:
        if namespace.document is not None:
            self.parent.diagnostics.force_refresh_document(namespace.document)

    @language_id("robotframework")
    def _on_namespace_invalidated(self, sender: Any, namespace: Namespace) -> None:
        if namespace.document is not None:
            namespace.document.remove_cache_entry(self._collect_model_errors)
            namespace.document.remove_cache_entry(self._collect_token_errors)

    @language_id("robotframework")
    def _on_get_related_documents(self, sender: Any, document: TextDocument) -> Optional[List[TextDocument]]:
        namespace = self.parent.documents_cache.get_only_initialized_namespace(document)
        if namespace is None:
            return None

        result = []

        resources = namespace.get_resources().values()
        for r in resources:
            if r.library_doc.source:
                doc = self.parent.documents.get(Uri.from_path(r.library_doc.source).normalized())
                if doc is not None:
                    result.append(doc)

        lib_doc = namespace.get_library_doc()
        for doc in self.parent.documents.documents:
            if doc.language_id != "robotframework":
                continue

            doc_namespace = self.parent.documents_cache.get_only_initialized_namespace(doc)
            if doc_namespace is None:
                continue

            if doc_namespace.is_analyzed():
                for ref in doc_namespace.get_namespace_references():
                    if ref.library_doc == lib_doc:
                        result.append(doc)

        return result

    def modify_diagnostics(self, document: TextDocument, diagnostics: List[Diagnostic]) -> List[Diagnostic]:
        return self.parent.documents_cache.get_diagnostic_modifier(document).modify_diagnostics(diagnostics)

    @language_id("robotframework")
    def collect_namespace_diagnostics(
        self, sender: Any, document: TextDocument, diagnostics_type: DiagnosticsCollectType
    ) -> DiagnosticsResult:
        try:
            namespace = self.parent.documents_cache.get_namespace(document)

            return DiagnosticsResult(
                self.collect_namespace_diagnostics, self.modify_diagnostics(document, namespace.get_diagnostics())
            )
        except (CancelledError, SystemExit, KeyboardInterrupt):
            raise
        except BaseException as e:
            self._logger.exception(e)
            return DiagnosticsResult(
                self.collect_namespace_diagnostics,
                [
                    Diagnostic(
                        range=Range(
                            start=Position(line=0, character=0),
                            end=Position(
                                line=len(document.get_lines()),
                                character=len((document.get_lines())[-1] or ""),
                            ),
                        ),
                        message=f"Fatal: can't get namespace diagnostics '{e}' ({type(e).__qualname__})",
                        severity=DiagnosticSeverity.ERROR,
                        source=self.source_name,
                        code=type(e).__qualname__,
                    )
                ],
            )

    def _create_error_from_node(
        self,
        node: ast.AST,
        msg: str,
        source: Optional[str] = None,
        only_start: bool = True,
    ) -> Diagnostic:
        from robot.parsing.model.statements import Statement

        if isinstance(node, HeaderAndBodyBlock):
            if node.header is not None:
                node = node.header
            elif node.body:
                stmt = next((n for n in node.body if isinstance(n, Statement)), None)
                if stmt is not None:
                    node = stmt

        return Diagnostic(
            range=range_from_node(node, True, only_start),
            message=msg,
            severity=DiagnosticSeverity.ERROR,
            source=source if source is not None else self.source_name,
            code="ModelError",
        )

    def _create_error_from_token(self, token: Token, source: Optional[str] = None) -> Diagnostic:
        return Diagnostic(
            range=range_from_token(token),
            message=token.error if token.error is not None else "(No Message).",
            severity=DiagnosticSeverity.ERROR,
            source=source if source is not None else self.source_name,
            code="TokenError",
        )

    @language_id("robotframework")
    @_logger.call
    def collect_token_errors(
        self, sender: Any, document: TextDocument, diagnostics_type: DiagnosticsCollectType
    ) -> DiagnosticsResult:
        return document.get_cache(self._collect_token_errors)

    def _collect_token_errors(self, document: TextDocument) -> DiagnosticsResult:
        from robot.errors import VariableError
        from robot.parsing.lexer.tokens import Token

        result: List[Diagnostic] = []
        try:
            for token in self.parent.documents_cache.get_tokens(document):
                check_current_task_canceled()

                if token.type in [Token.ERROR, Token.FATAL_ERROR]:
                    result.append(self._create_error_from_token(token))

                try:
                    for variable_token in token.tokenize_variables():
                        if variable_token == token:
                            break

                        if variable_token.type in [Token.ERROR, Token.FATAL_ERROR]:
                            result.append(self._create_error_from_token(variable_token))

                except VariableError as e:
                    result.append(
                        Diagnostic(
                            range=range_from_token(token),
                            message=str(e),
                            severity=DiagnosticSeverity.ERROR,
                            source=self.source_name,
                            code=type(e).__qualname__,
                        )
                    )
        except (CancelledError, SystemExit, KeyboardInterrupt):
            raise
        except BaseException as e:
            return DiagnosticsResult(
                self.collect_token_errors,
                [
                    Diagnostic(
                        range=Range(
                            start=Position(line=0, character=0),
                            end=Position(
                                line=len(document.get_lines()),
                                character=len((document.get_lines())[-1] or ""),
                            ),
                        ),
                        message=f"Fatal: can't get token diagnostics '{e}' ({type(e).__qualname__})",
                        severity=DiagnosticSeverity.ERROR,
                        source=self.source_name,
                        code=type(e).__qualname__,
                    )
                ],
            )

        return DiagnosticsResult(self.collect_token_errors, self.modify_diagnostics(document, result))

    @language_id("robotframework")
    @_logger.call
    def collect_model_errors(
        self, sender: Any, document: TextDocument, diagnostics_type: DiagnosticsCollectType
    ) -> DiagnosticsResult:
        return document.get_cache(self._collect_model_errors)

    def _collect_model_errors(self, document: TextDocument) -> DiagnosticsResult:
        try:
            model = self.parent.documents_cache.get_model(document, True)

            result: List[Diagnostic] = []
            for node in iter_nodes(model):
                check_current_task_canceled()

                error = node.error if isinstance(node, HasError) else None
                if error is not None:
                    result.append(self._create_error_from_node(node, error))
                errors = node.errors if isinstance(node, HasErrors) else None
                if errors is not None:
                    for e in errors:
                        result.append(self._create_error_from_node(node, e))

            return DiagnosticsResult(self.collect_model_errors, self.modify_diagnostics(document, result))

        except (CancelledError, SystemExit, KeyboardInterrupt):
            raise
        except BaseException as e:
            return DiagnosticsResult(
                self.collect_model_errors,
                [
                    Diagnostic(
                        range=Range(
                            start=Position(line=0, character=0),
                            end=Position(
                                line=len(document.get_lines()),
                                character=len((document.get_lines())[-1] or ""),
                            ),
                        ),
                        message=f"Fatal: can't get model diagnostics '{e}' ({type(e).__qualname__})",
                        severity=DiagnosticSeverity.ERROR,
                        source=self.source_name,
                        code=type(e).__qualname__,
                    )
                ],
            )

    @language_id("robotframework")
    @_logger.call
    def collect_unused_keyword_references(
        self, sender: Any, document: TextDocument, diagnostics_type: DiagnosticsCollectType
    ) -> DiagnosticsResult:
        config = self.parent.workspace.get_configuration(AnalysisConfig, document.uri)

        if not config.find_unused_references:
            return DiagnosticsResult(self.collect_unused_keyword_references, [])

        if diagnostics_type != DiagnosticsCollectType.SLOW:
            return DiagnosticsResult(self.collect_unused_keyword_references, None, True)

        return self._collect_unused_keyword_references(document)

    def _collect_unused_keyword_references(self, document: TextDocument) -> DiagnosticsResult:
        try:
            namespace = self.parent.documents_cache.get_namespace(document)

            result: List[Diagnostic] = []
            for kw in (namespace.get_library_doc()).keywords.values():
                check_current_task_canceled()

                references = self.parent.robot_references.find_keyword_references(document, kw, False, True)
                if not references:
                    result.append(
                        Diagnostic(
                            range=kw.name_range,
                            message=f"Keyword '{kw.name}' is not used.",
                            severity=DiagnosticSeverity.WARNING,
                            source=self.source_name,
                            code="KeywordNotUsed",
                            tags=[DiagnosticTag.UNNECESSARY],
                        )
                    )

            return DiagnosticsResult(self.collect_unused_keyword_references, self.modify_diagnostics(document, result))
        except (CancelledError, SystemExit, KeyboardInterrupt):
            raise
        except BaseException as e:
            return DiagnosticsResult(
                self.collect_unused_keyword_references,
                [
                    Diagnostic(
                        range=Range(
                            start=Position(line=0, character=0),
                            end=Position(
                                line=len(document.get_lines()),
                                character=len((document.get_lines())[-1] or ""),
                            ),
                        ),
                        message=f"Fatal: can't collect unused keyword references '{e}' ({type(e).__qualname__})",
                        severity=DiagnosticSeverity.ERROR,
                        source=self.source_name,
                        code=type(e).__qualname__,
                    )
                ],
            )

    @language_id("robotframework")
    @_logger.call
    def collect_unused_variable_references(
        self, sender: Any, document: TextDocument, diagnostics_type: DiagnosticsCollectType
    ) -> DiagnosticsResult:
        config = self.parent.workspace.get_configuration(AnalysisConfig, document.uri)

        if not config.find_unused_references:
            return DiagnosticsResult(self.collect_unused_variable_references, [])

        if diagnostics_type != DiagnosticsCollectType.SLOW:
            return DiagnosticsResult(self.collect_unused_variable_references, None, True)

        return self._collect_unused_variable_references(document)

    def _collect_unused_variable_references(self, document: TextDocument) -> DiagnosticsResult:
        try:
            namespace = self.parent.documents_cache.get_namespace(document)

            result: List[Diagnostic] = []

            for var in (namespace.get_variable_references()).keys():
                check_current_task_canceled()

                if isinstance(
                    var, (LibraryArgumentDefinition, EnvironmentVariableDefinition, GlobalVariableDefinition)
                ):
                    continue

                if var.name_token is not None and var.name_token.value and var.name_token.value.startswith("_"):
                    continue

                references = self.parent.robot_references.find_variable_references(document, var, False, True)
                if not references:
                    result.append(
                        Diagnostic(
                            range=var.name_range,
                            message=f"{'Argument' if isinstance(var, ArgumentDefinition) else 'Variable'}"
                            f" '{var.name}' is not used.",
                            severity=DiagnosticSeverity.WARNING,
                            source=self.source_name,
                            code="VariableNotUsed",
                            tags=[DiagnosticTag.UNNECESSARY],
                        )
                    )

            return DiagnosticsResult(self.collect_unused_variable_references, self.modify_diagnostics(document, result))
        except (CancelledError, SystemExit, KeyboardInterrupt):
            raise
        except BaseException as e:
            return DiagnosticsResult(
                self.collect_unused_variable_references,
                [
                    Diagnostic(
                        range=Range(
                            start=Position(line=0, character=0),
                            end=Position(
                                line=len(document.get_lines()),
                                character=len((document.get_lines())[-1] or ""),
                            ),
                        ),
                        message=f"Fatal: can't collect unused variable references '{e}' ({type(e).__qualname__})",
                        severity=DiagnosticSeverity.ERROR,
                        source=self.source_name,
                        code=type(e).__qualname__,
                    )
                ],
            )
