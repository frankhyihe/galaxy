"""
Data loading agent for automated SRA data retrieval and collection organization.

This agent uses galaxy-mcp (Model Context Protocol server for Galaxy) to execute
the fasterq_dump tool for downloading SRA data, ensuring full replicability by
routing all tool execution through MCP rather than direct Galaxy API calls.
"""

import csv
import io
import logging
from pathlib import Path
from typing import (
    Any,
    Optional,
)

from pydantic_ai import Agent
from pydantic_ai.mcp import MCPServerStdio
from pydantic_ai.tools import RunContext

from galaxy.schema.agents import ConfidenceLevel
from .base import (
    ActionSuggestion,
    ActionType,
    AgentResponse,
    AgentType,
    BaseGalaxyAgent,
    extract_result_content,
    GalaxyAgentDependencies,
)

log = logging.getLogger(__name__)

# The specific fasterq_dump tool version this agent is designed to work with
FASTERQ_DUMP_TOOL_ID = (
    "toolshed.g2.bx.psu.edu/repos/iuc/sra_tools/fasterq_dump/3.1.1+galaxy1"
)


class DataLoadingAgent(BaseGalaxyAgent):
    """
    Agent for automated SRA data loading and collection organization.

    This agent parses user-provided metadata tables (CSV/TSV) containing SRA
    sample information, downloads FASTQ data using the fasterq_dump tool via
    galaxy-mcp, and organizes the resulting datasets into appropriate Galaxy
    collections (single or grouped by experimental condition).

    Key capabilities:
    - Parse SRA metadata tables to identify accessions and layout (paired/single)
    - Detect experimental groups from metadata columns (e.g., Library Name)
    - Execute fasterq_dump for each accession via galaxy-mcp
    - Organize outputs into paired or unpaired collections
    - Group collections by experimental condition when applicable

    All tool execution goes through galaxy-mcp for full replicability.
    """

    agent_type = AgentType.DATA_LOADING

    def _get_galaxy_mcp_env(self) -> dict[str, str]:
        """Build environment variables for the galaxy-mcp subprocess.

        Retrieves the Galaxy instance URL from server config and the user's
        API key from their account to authenticate the MCP server connection.
        """
        env: dict[str, str] = {}

        # Galaxy instance URL from server configuration
        galaxy_url = getattr(self.deps.config, "galaxy_infrastructure_url", None)
        if galaxy_url:
            env["GALAXY_URL"] = galaxy_url

        # User API key for authentication
        if self.deps.user and hasattr(self.deps.user, "api_keys"):
            active_keys = [k for k in self.deps.user.api_keys if not k.deleted]
            if active_keys:
                env["GALAXY_API_KEY"] = active_keys[0].key

        return env

    def _create_mcp_server(self) -> MCPServerStdio:
        """Create the galaxy-mcp stdio server instance.

        Uses ``uvx galaxy-mcp`` to launch the MCP server as a subprocess,
        passing Galaxy connection credentials via environment variables.
        """
        env = self._get_galaxy_mcp_env()
        return MCPServerStdio(
            "uvx",
            args=["galaxy-mcp"],
            env=env if env else None,
            timeout=120,
        )

    def _create_agent(self) -> Agent[GalaxyAgentDependencies, Any]:
        """Create the data loading agent with galaxy-mcp as an MCP toolset.

        The agent is configured with:
        1. galaxy-mcp MCP server providing Galaxy tool execution capabilities
           (run_tool, create_history, get_histories, upload_file, etc.)
        2. Local tools for reading dataset content and creating collections
           (operations that require direct access to Galaxy internals)
        """
        mcp_server = self._create_mcp_server()

        agent = Agent(
            self._get_model(),
            deps_type=GalaxyAgentDependencies,
            system_prompt=self.get_system_prompt(),
            toolsets=[mcp_server],
        )

        # Register local tools that need direct Galaxy access
        self._register_local_tools(agent)

        return agent

    def _register_local_tools(self, agent: Agent[GalaxyAgentDependencies, Any]) -> None:
        """Register tools that require direct Galaxy server access.

        These supplement the galaxy-mcp tools with operations that need
        access to Galaxy internals (reading dataset content, building
        dataset collections).
        """

        @agent.tool
        async def read_dataset_as_text(
            ctx: RunContext[GalaxyAgentDependencies],
            dataset_id: str,
            max_lines: int = 100,
        ) -> str:
            """Read the text content of a dataset from the current Galaxy history.

            Use this to read metadata tables (CSV/TSV) that the user has uploaded
            to Galaxy. The content is returned as plain text that you can parse.

            Args:
                dataset_id: The encoded Galaxy dataset ID (HDA ID).
                max_lines: Maximum number of lines to read. Default 100.
                           Set higher if the metadata table is large.

            Returns:
                The text content of the dataset, truncated to max_lines.
            """
            try:
                if not ctx.deps.dataset_manager:
                    return "Error: Dataset manager not available."

                dataset = ctx.deps.dataset_manager.get_accessible(
                    dataset_id, ctx.deps.trans
                )
                if not dataset:
                    return f"Error: Dataset {dataset_id} not found or not accessible."

                # Read the file content
                file_name = dataset.dataset.get_file_name()
                lines = []
                with open(file_name, encoding="utf-8", errors="replace") as f:
                    for i, line in enumerate(f):
                        if i >= max_lines:
                            lines.append(
                                f"... (truncated at {max_lines} lines, "
                                f"file may contain more rows)"
                            )
                            break
                        lines.append(line.rstrip("\n"))

                return "\n".join(lines)

            except Exception as e:
                log.warning(f"Error reading dataset {dataset_id}: {e}")
                return f"Error reading dataset: {str(e)}"

        @agent.tool
        async def parse_metadata_table(
            ctx: RunContext[GalaxyAgentDependencies],
            dataset_id: str,
        ) -> str:
            """Parse a CSV/TSV metadata table from a Galaxy dataset and return structured information.

            This tool reads the metadata file, auto-detects the delimiter (tab or comma),
            identifies key columns (Run Accession, Fastq FTP, Library Layout, Library Name),
            and returns a structured summary including:
            - Column names detected
            - Number of samples
            - Whether data is paired-end or single-end
            - Experimental groups (if Library Name column exists)
            - SRA accession list

            Args:
                dataset_id: The encoded Galaxy dataset ID containing the metadata table.

            Returns:
                A structured text summary of the metadata that can be used to plan
                the data loading workflow.
            """
            try:
                if not ctx.deps.dataset_manager:
                    return "Error: Dataset manager not available."

                dataset = ctx.deps.dataset_manager.get_accessible(
                    dataset_id, ctx.deps.trans
                )
                if not dataset:
                    return f"Error: Dataset {dataset_id} not found or not accessible."

                file_name = dataset.dataset.get_file_name()
                with open(file_name, encoding="utf-8", errors="replace") as f:
                    content = f.read()

                # Auto-detect delimiter
                first_line = content.split("\n")[0]
                if "\t" in first_line:
                    delimiter = "\t"
                    delimiter_name = "tab"
                elif "," in first_line:
                    delimiter = ","
                    delimiter_name = "comma"
                else:
                    delimiter = "\t"
                    delimiter_name = "tab (assumed)"

                reader = csv.DictReader(io.StringIO(content), delimiter=delimiter)
                rows = list(reader)
                headers = reader.fieldnames or []

                if not rows:
                    return "Error: Metadata table is empty (no data rows found)."

                # Analyze the metadata
                result_parts = [
                    f"## Metadata Table Summary",
                    f"- **Delimiter**: {delimiter_name}",
                    f"- **Columns** ({len(headers)}): {', '.join(headers)}",
                    f"- **Rows**: {len(rows)} samples",
                ]

                # Detect SRA accession column
                accession_col = None
                for col_name in [
                    "Run Accession",
                    "Run",
                    "SRA Accession",
                    "Accession",
                    "run_accession",
                    "run",
                    "sra_accession",
                    "accession",
                    "Run_Accession",
                ]:
                    if col_name in headers:
                        accession_col = col_name
                        break

                if accession_col:
                    accessions = [
                        row.get(accession_col, "").strip()
                        for row in rows
                        if row.get(accession_col, "").strip()
                    ]
                    result_parts.append(
                        f"- **SRA Accessions** (column: '{accession_col}'): "
                        f"{', '.join(accessions)}"
                    )
                else:
                    result_parts.append(
                        "- **WARNING**: No SRA accession column detected. "
                        "Expected column names: 'Run Accession', 'Run', 'Accession'"
                    )

                # Detect paired/single end layout
                is_paired = False
                layout_source = "unknown"

                # Check Library Layout column
                layout_col = None
                for col_name in [
                    "Library Layout",
                    "library_layout",
                    "LibraryLayout",
                    "Layout",
                ]:
                    if col_name in headers:
                        layout_col = col_name
                        break

                if layout_col:
                    layouts = set(
                        row.get(layout_col, "").strip().upper()
                        for row in rows
                        if row.get(layout_col, "").strip()
                    )
                    is_paired = "PAIRED" in layouts
                    layout_source = (
                        f"Library Layout column (values: {', '.join(layouts)})"
                    )
                else:
                    # Check Fastq FTP column for paired URLs (semicolon-separated)
                    ftp_col = None
                    for col_name in [
                        "Fastq FTP",
                        "fastq_ftp",
                        "Fastq_FTP",
                        "FASTQ_FTP",
                        "FTP",
                    ]:
                        if col_name in headers:
                            ftp_col = col_name
                            break

                    if ftp_col:
                        sample_ftp = rows[0].get(ftp_col, "")
                        if ";" in sample_ftp:
                            is_paired = True
                            layout_source = (
                                f"Fastq FTP column contains paired URLs "
                                f"(semicolon-separated)"
                            )
                        else:
                            layout_source = "Fastq FTP column (single URL per row)"

                result_parts.append(
                    f"- **Library Layout**: "
                    f"{'PAIRED' if is_paired else 'SINGLE'} "
                    f"(detected from: {layout_source})"
                )

                # Detect experimental groups
                group_col = None
                for col_name in [
                    "Library Name",
                    "library_name",
                    "LibraryName",
                    "Sample Name",
                    "sample_name",
                    "SampleName",
                    "Experiment",
                    "experiment",
                    "Group",
                    "group",
                    "Condition",
                    "condition",
                ]:
                    if col_name in headers:
                        group_col = col_name
                        break

                if group_col:
                    library_names = [
                        row.get(group_col, "").strip()
                        for row in rows
                        if row.get(group_col, "").strip()
                    ]

                    # Try to extract group prefixes (e.g., AR0382_A -> AR0382)
                    groups: dict[str, list[str]] = {}
                    for name in library_names:
                        # Split on common suffixes like _A, _B, _1, _2, _rep1
                        parts = name.rsplit("_", 1)
                        if len(parts) == 2 and parts[1] in (
                            "A",
                            "B",
                            "C",
                            "D",
                            "1",
                            "2",
                            "3",
                            "4",
                            "rep1",
                            "rep2",
                            "rep3",
                            "rep4",
                            "Rep1",
                            "Rep2",
                            "Rep3",
                            "Rep4",
                        ):
                            group_name = parts[0]
                        else:
                            group_name = name
                        groups.setdefault(group_name, []).append(name)

                    if len(groups) > 1:
                        result_parts.append(
                            f"\n- **Experimental Groups** "
                            f"(column: '{group_col}', {len(groups)} groups):"
                        )
                        for group_name, members in sorted(groups.items()):
                            result_parts.append(
                                f"  - **{group_name}**: {', '.join(members)} "
                                f"({len(members)} replicates)"
                            )
                    else:
                        result_parts.append(
                            f"- **Library Names** (column: '{group_col}'): "
                            f"{', '.join(library_names)}"
                        )
                        result_parts.append(
                            "  (All samples appear to belong to a single group)"
                        )
                else:
                    result_parts.append(
                        "- **Experimental Groups**: No grouping column detected. "
                        "All samples will be placed in a single collection."
                    )

                # Provide action recommendation
                result_parts.append("\n## Recommended Action")
                if accession_col and accessions:
                    if is_paired:
                        if group_col and len(groups) > 1:
                            result_parts.append(
                                f"Create {len(groups)} paired collections, "
                                f"one per experimental group, using fasterq_dump "
                                f"on {len(accessions)} SRA accessions."
                            )
                        else:
                            result_parts.append(
                                f"Create 1 paired collection from "
                                f"{len(accessions)} SRA accessions using fasterq_dump."
                            )
                    else:
                        result_parts.append(
                            f"Create a single-end collection from "
                            f"{len(accessions)} SRA accessions using fasterq_dump."
                        )
                else:
                    result_parts.append(
                        "Cannot determine action: missing SRA accession column."
                    )

                return "\n".join(result_parts)

            except Exception as e:
                log.warning(f"Error parsing metadata from dataset {dataset_id}: {e}")
                return f"Error parsing metadata table: {str(e)}"

        @agent.tool
        async def create_dataset_collection(
            ctx: RunContext[GalaxyAgentDependencies],
            history_id: str,
            collection_name: str,
            element_identifiers: list[dict[str, Any]],
            collection_type: str = "list:paired",
        ) -> str:
            """Create a dataset collection in a Galaxy history.

            Use this to organize fasterq_dump output datasets into structured
            collections (paired or unpaired).

            For a paired collection (collection_type="list:paired"), each element
            in element_identifiers should be a dict with:
            - "name": The sample name (e.g., "SRR22376027")
            - "src": "new_collection"
            - "collection_type": "paired"
            - "element_identifiers": [
                {"name": "forward", "src": "hda", "id": "<forward_dataset_id>"},
                {"name": "reverse", "src": "hda", "id": "<reverse_dataset_id>"}
              ]

            For a simple list collection (collection_type="list"), each element
            should be:
            - {"name": "sample_name", "src": "hda", "id": "<dataset_id>"}

            Args:
                history_id: The Galaxy history ID containing the datasets.
                collection_name: Display name for the collection.
                element_identifiers: List of element definitions (see above).
                collection_type: Type of collection. Common values:
                    - "list:paired" for paired-end data (most common for SRA)
                    - "list" for single-end data
                    - "paired" for a single pair

            Returns:
                A message confirming collection creation with the collection ID,
                or an error message if creation failed.
            """
            try:
                trans = ctx.deps.trans
                app = trans.app  # type: ignore[union-attr]
                collection_service = app.dataset_collections_service

                # Build the create payload
                create_params = dict(
                    collection_type=collection_type,
                    name=collection_name,
                    hide_source_items=False,
                    element_identifiers=element_identifiers,
                )

                collection_info = collection_service.create(
                    trans=trans,
                    parent=trans.history,
                    payload=create_params,
                )

                collection_id = getattr(collection_info, "id", "unknown")
                element_count = getattr(
                    getattr(collection_info, "collection", None),
                    "element_count",
                    len(element_identifiers),
                )

                return (
                    f"Successfully created collection '{collection_name}' "
                    f"(ID: {collection_id}) with {element_count} elements "
                    f"of type '{collection_type}' in history {history_id}."
                )

            except Exception as e:
                log.error(f"Error creating collection '{collection_name}': {e}")
                return (
                    f"Error creating collection '{collection_name}': {str(e)}. "
                    f"Please verify that the dataset IDs are correct and exist "
                    f"in the specified history."
                )

    def get_system_prompt(self) -> str:
        """Get the system prompt for the data loading agent."""
        prompt_path = Path(__file__).parent / "prompts" / "data_loading.md"
        return prompt_path.read_text()

    async def process(
        self, query: str, context: Optional[dict[str, Any]] = None
    ) -> AgentResponse:
        """Process a data loading request.

        The agent interprets the user's request (typically involving an uploaded
        metadata table), plans the data loading workflow, and executes it using
        galaxy-mcp for tool runs and local tools for collection creation.

        Args:
            query: The user's request describing what data to load.
            context: Additional context, which may include:
                - dataset_id: ID of an uploaded metadata table
                - history_id: Target history for loaded data
                - conversation_history: Previous conversation messages

        Returns:
            AgentResponse with the result of the data loading operation.
        """
        try:
            # Enhance query with context information
            enhanced_query = query
            if context:
                context_parts = []
                if context.get("dataset_id"):
                    context_parts.append(
                        f"Metadata dataset ID: {context['dataset_id']}"
                    )
                if context.get("history_id"):
                    context_parts.append(f"Target history ID: {context['history_id']}")
                if context_parts:
                    enhanced_query = (
                        "Context:\n"
                        + "\n".join(context_parts)
                        + f"\n\nUser request: {query}"
                    )

            result = await self._run_with_retry(enhanced_query)
            content = extract_result_content(result)

            return AgentResponse(
                content=content,
                confidence=ConfidenceLevel.HIGH,
                agent_type=self.agent_type,
                suggestions=[
                    ActionSuggestion(
                        action_type=ActionType.DATA_FETCH,
                        description="Data loading completed via fasterq_dump",
                        confidence=ConfidenceLevel.HIGH,
                        priority=1,
                    )
                ],
                metadata={
                    "tool_id": FASTERQ_DUMP_TOOL_ID,
                    "method": "galaxy_mcp",
                },
            )

        except OSError as e:
            log.warning(f"Data loading network error: {e}")
            return self._get_fallback_response(query, str(e))
        except ValueError as e:
            log.warning(f"Data loading value error: {e}")
            return self._get_fallback_response(query, str(e))

    def _get_fallback_content(self) -> str:
        """Get fallback content for data loading failures."""
        return (
            "Unable to complete data loading at this time. "
            "Please ensure that the galaxy-mcp server is accessible and that "
            "the fasterq_dump tool is installed on this Galaxy instance."
        )
