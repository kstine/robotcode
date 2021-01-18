import uuid
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union

from ...jsonrpc2.protocol import JsonRPCProtocol, JsonRPCProtocolPart, rpc_method
from ...utils.async_event import async_event
from ...utils.logging import LoggingDescriptor
from ...utils.uri import Uri
from ..types import (
    ConfigurationItem,
    ConfigurationParams,
    CreateFilesParams,
    DeleteFilesParams,
    DidChangeConfigurationParams,
    FileCreate,
    FileDelete,
    FileOperationFilter,
    FileOperationPattern,
    FileOperationRegistrationOptions,
    FileRename,
    RenameFilesParams,
    ServerCapabilities,
    TextEdit,
    WorkspaceEdit,
    WorkspaceFolder,
    WorkspaceFoldersServerCapabilities,
)


class Workspace(JsonRPCProtocolPart):
    _logger = LoggingDescriptor()

    def __init__(
        self,
        parent: JsonRPCProtocol,
        root_uri: Optional[str],
        root_path: Optional[str],
        workspace_folders: Optional[List[WorkspaceFolder]] = None,
    ):
        super().__init__(parent)
        self.root_uri = root_uri
        self.root_path = root_path
        self.workspace_folders: List[WorkspaceFolder] = workspace_folders or []
        self._settings: Dict[str, Any] = {}

    def extend_capabilities(self, capabilities: ServerCapabilities) -> None:
        capabilities.workspace = ServerCapabilities.Workspace(
            workspace_folders=WorkspaceFoldersServerCapabilities(
                supported=True, change_notifications=str(uuid.uuid4())
            ),
            file_operations=ServerCapabilities.Workspace.FileOperations(
                did_create=FileOperationRegistrationOptions(
                    filters=[FileOperationFilter(pattern=FileOperationPattern(glob="**/*"))]
                ),
                will_create=FileOperationRegistrationOptions(
                    filters=[FileOperationFilter(pattern=FileOperationPattern(glob="**/*"))]
                ),
                did_rename=FileOperationRegistrationOptions(
                    filters=[FileOperationFilter(pattern=FileOperationPattern(glob="**/*"))]
                ),
                will_rename=FileOperationRegistrationOptions(
                    filters=[FileOperationFilter(pattern=FileOperationPattern(glob="**/*"))]
                ),
                did_delete=FileOperationRegistrationOptions(
                    filters=[FileOperationFilter(pattern=FileOperationPattern(glob="**/*"))]
                ),
                will_delete=FileOperationRegistrationOptions(
                    filters=[FileOperationFilter(pattern=FileOperationPattern(glob="**/*"))]
                ),
            ),
        )

    @property
    def settings(self) -> Dict[str, Any]:
        return self._settings

    @settings.setter
    def settings(self, value: Dict[str, Any]) -> None:
        self._settings = value

    @async_event
    async def did_change_configuration(sender, settings: Dict[str, Any]) -> None:
        ...

    @rpc_method(name="workspace/didChangeConfiguration", param_type=DidChangeConfigurationParams)
    @_logger.call
    async def _workspace_did_change_configuration(self, settings: Dict[str, Any], *args: Any, **kwargs: Any) -> None:
        self.settings = settings
        await self.did_change_configuration(self, settings)

    @async_event
    async def will_create_files(sender, files: List[str]) -> Mapping[str, TextEdit]:
        ...

    @async_event
    async def did_create_files(sender, files: List[str]) -> None:
        ...

    @async_event
    async def will_rename_files(sender, files: List[Tuple[str, str]]) -> None:
        ...

    @async_event
    async def did_rename_files(sender, files: List[Tuple[str, str]]) -> None:
        ...

    @async_event
    async def will_delete_files(sender, files: List[str]) -> None:
        ...

    @async_event
    async def did_delete_files(sender, files: List[str]) -> None:
        ...

    @rpc_method(name="workspace/willCreateFiles", param_type=CreateFilesParams)
    @_logger.call
    async def _workspace_will_create_files(
        self, files: List[FileCreate], *args: Any, **kwargs: Any
    ) -> Optional[WorkspaceEdit]:
        results = await self.will_create_files(self, list(f.uri for f in files))
        if len(results) == 0:
            return None

        result: Dict[str, List[TextEdit]] = {}
        for e in results:
            if e is not None and isinstance(e, Mapping):
                result.update(e)

        # TODO: support full WorkspaceEdit

        return WorkspaceEdit(changes=result)

    @rpc_method(name="workspace/didCreateFiles", param_type=CreateFilesParams)
    @_logger.call
    async def _workspace_did_create_files(self, files: List[FileCreate], *args: Any, **kwargs: Any) -> None:
        await self.did_create_files(self, list(f.uri for f in files))

    @rpc_method(name="workspace/willRenameFiles", param_type=RenameFilesParams)
    @_logger.call
    async def _workspace_will_rename_files(self, files: List[FileRename], *args: Any, **kwargs: Any) -> None:
        await self.will_rename_files(self, list((f.old_uri, f.new_uri) for f in files))

        # TODO: return WorkspaceEdit

    @rpc_method(name="workspace/didRenameFiles", param_type=RenameFilesParams)
    @_logger.call
    async def _workspace_did_rename_files(self, files: List[FileRename], *args: Any, **kwargs: Any) -> None:
        await self.did_rename_files(self, list((f.old_uri, f.new_uri) for f in files))

    @rpc_method(name="workspace/willDeleteFiles", param_type=DeleteFilesParams)
    @_logger.call
    async def _workspace_will_delete_files(self, files: List[FileDelete], *args: Any, **kwargs: Any) -> None:
        await self.will_delete_files(self, list(f.uri for f in files))

        # TODO: return WorkspaceEdit

    @rpc_method(name="workspace/didDeleteFiles", param_type=DeleteFilesParams)
    @_logger.call
    async def _workspace_did_delete_files(self, files: List[FileDelete], *args: Any, **kwargs: Any) -> None:
        await self.did_delete_files(self, list(f.uri for f in files))

    async def get_configuration(self, section: str, scope_uri: Union[str, Uri, None] = None) -> Any:
        return (
            await self.parent.send_request(
                "workspace/configuration",
                ConfigurationParams(
                    items=[
                        ConfigurationItem(
                            scope_uri=str(scope_uri) if isinstance(scope_uri, Uri) else scope_uri, section=section
                        )
                    ]
                ),
                list,
            )
        )[0]

    def get_workspace_folder(self, uri: Union[Uri, str]) -> Optional[WorkspaceFolder]:
        if isinstance(uri, str):
            uri = Uri(uri)

        uri_path = uri.to_path()
        result = sorted(
            [f for f in self.workspace_folders if uri_path.is_relative_to(Uri(f.uri).to_path())],
            key=lambda v1: len(v1.uri),
            reverse=True,
        )

        if len(result) > 0:
            return result[0]

        return None
