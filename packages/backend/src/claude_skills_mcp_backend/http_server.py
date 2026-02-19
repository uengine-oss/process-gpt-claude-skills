"""HTTP server with MCP Streamable HTTP transport using FastMCP."""

import asyncio
import base64
import logging
import re
import shutil
import tempfile
import threading
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx
import uvicorn
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import TextContent

from .search_engine import SkillSearchEngine
from .skill_loader import (
    Skill,
    load_skills_in_batches,
    load_all_skills,
    load_from_local,
    parse_skill_md,
)
from .config import load_config
from .update_checker import UpdateChecker
from .scheduler import HourlyScheduler

logger = logging.getLogger(__name__)

# Create FastMCP server
# mcp = FastMCP("claude-skills-mcp-backend")
mcp = FastMCP(
    "claude-skills-mcp-backend",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            "localhost:*",
            "127.0.0.1:*",
            "claude-skills:*",
            "claude-skills:8765",
            "claude-skills.dev.svc:*",
            "claude-skills.dev.svc.cluster.local:*",
        ],
        allowed_origins=[
            "http://localhost:*",
            "http://127.0.0.1:*",
            "http://claude-skills:*",
            "http://claude-skills.dev.svc:*",
            "http://claude-skills.dev.svc.cluster.local:*",
        ],
    ),
)


# Global state
search_engine: SkillSearchEngine | None = None
loading_state_global = None
update_checker_global: UpdateChecker | None = None
scheduler_global: HourlyScheduler | None = None
config_global: dict[str, Any] | None = None
reload_lock: asyncio.Lock | None = None
_routes_initialized = False


class LoadingState:
    """Thread-safe state tracker for background skill loading."""

    def __init__(self):
        self.total_skills = 0
        self.loaded_skills = 0
        self.is_complete = False
        self.errors: list[str] = []
        self._lock = threading.Lock()

    def update_progress(self, loaded: int, total: int | None = None) -> None:
        with self._lock:
            self.loaded_skills = loaded
            if total is not None:
                self.total_skills = total

    def add_error(self, error: str) -> None:
        with self._lock:
            self.errors.append(error)

    def mark_complete(self) -> None:
        with self._lock:
            self.is_complete = True

    def get_status_message(self) -> str | None:
        with self._lock:
            if self.is_complete:
                return None
            if self.loaded_skills == 0:
                return "[LOADING: Skills are being loaded in the background, please wait...]\n"
            if self.total_skills > 0:
                return f"[LOADING: {self.loaded_skills}/{self.total_skills} skills loaded, indexing in progress...]\n"
            return f"[LOADING: {self.loaded_skills} skills loaded so far, indexing in progress...]\n"


def _slugify(value: str) -> str:
    """Create filesystem-friendly slug from skill name."""
    slug = re.sub(r"[^\w\s-]", "", value.lower()).strip()
    slug = re.sub(r"[-\s]+", "-", slug)
    return slug or "skill"


def _get_primary_local_skill_root() -> Path | None:
    """Get primary local skills path from configuration or environment variable.
    
    Priority:
    1. Environment variable SKILLS_STORAGE_PATH (for container deployments)
    2. First local source in skill_sources config
    """
    if not config_global:
        return None

    # Check environment variable first (for container deployments with mounted volumes)
    import os
    env_path = os.getenv("SKILLS_STORAGE_PATH")
    if env_path:
        path = Path(env_path).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        logger.info(f"Using skills storage path from environment: {path}")
        return path

    # Fall back to config
    for source in config_global.get("skill_sources", []):
        if source.get("type") == "local" and source.get("path"):
            path = Path(source["path"]).expanduser()
            path.mkdir(parents=True, exist_ok=True)
            return path
    return None


def _safe_extract_zip(zip_file: zipfile.ZipFile, destination: Path) -> None:
    """Safely extract ZIP contents to destination, preventing path traversal."""
    for member in zip_file.namelist():
        member_path = Path(member)
        if member_path.is_absolute() or any(part == ".." for part in member_path.parts):
            raise ValueError("ZIP archive contains unsafe paths")
    zip_file.extractall(destination)


def _find_skill_directory(
    skill_name: str, 
    tenant_id: str | None = None, 
    agent_id: str | None = None
) -> tuple[Path | None, Skill | None]:
    """Find skill directory by name and tenant_id in local storage.
    
    Skills are stored at tenant level only. agent_id is used for filtering
    but does not affect the storage path.
    
    Parameters:
        skill_name: Name of the skill to find
        tenant_id: Optional tenant ID to filter by (affects storage path)
        agent_id: Optional agent ID to filter by (only for metadata filtering)
    
    Returns:
        tuple[Path | None, Skill | None]: (skill_dir, skill) or (None, None) if not found
    """
    local_root = _get_primary_local_skill_root()
    skill_found = None
    skill_dir = None
    
    # First, try to find in search_engine to get the source path
    if search_engine:
        with search_engine._lock:
            for skill in search_engine.skills:
                if skill.name != skill_name:
                    continue
                # Match tenant_id and agent_id if provided
                if tenant_id is not None and skill.tenant_id != tenant_id:
                    continue
                if agent_id is not None and skill.agent_id != agent_id:
                    continue
                # Also match public skills (no tenant/agent) if no filters provided
                if tenant_id is None and agent_id is None:
                    # If no filters, accept any skill (public or filtered)
                    pass
                elif (tenant_id is None or agent_id is None) and (skill.tenant_id is not None or skill.agent_id is not None):
                    # Partial match: if one filter is provided, the skill must match that filter
                    if tenant_id is not None and skill.tenant_id != tenant_id:
                        continue
                    if agent_id is not None and skill.agent_id != agent_id:
                        continue
                
                skill_found = skill
                # skill.source is the SKILL.md file path, so get the parent directory
                if skill.source and isinstance(skill.source, str) and local_root:
                    source_path = Path(skill.source)
                    # Check if it's a file (SKILL.md) or directory
                    if source_path.is_file() and source_path.name == "SKILL.md":
                        skill_dir = source_path.parent
                    elif source_path.is_dir():
                        skill_dir = source_path
                    # Verify it's within local_root (uploaded skill)
                    if skill_dir:
                        try:
                            skill_dir.relative_to(local_root.resolve())
                        except ValueError:
                            skill_dir = None  # Built-in: not in local storage
                break

    # If not found via search_engine, search local storage with path-based lookup
    if local_root and (skill_dir is None or not skill_dir.exists()):
        # Build expected path based on tenant_id only
        skill_slug = _slugify(skill_name)
        expected_paths = []
        
        if tenant_id:
            # Try tenant path first
            expected_paths.append(local_root / _slugify(tenant_id) / skill_slug)
        # Also try root level (public skills)
        expected_paths.append(local_root / skill_slug)
        
        # Check expected paths first
        for expected_path in expected_paths:
            if expected_path.exists():
                skill_file = expected_path / "SKILL.md"
                if skill_file.exists():
                    try:
                        content = skill_file.read_text(encoding="utf-8")
                        parsed = parse_skill_md(content, str(skill_file))
                        if parsed and parsed.name == skill_name:
                            # Verify tenant_id and agent_id match if provided
                            if tenant_id is not None and parsed.tenant_id != tenant_id:
                                continue
                            if agent_id is not None and parsed.agent_id != agent_id:
                                continue
                            skill_dir = expected_path
                            skill_found = parsed
                            break
                    except Exception:
                        continue
        
        # If still not found, search all subdirectories (fallback)
        if skill_dir is None:
            found_dirs = []
            for subdir in local_root.rglob("*"):
                if not subdir.is_dir():
                    continue
                skill_file = subdir / "SKILL.md"
                if skill_file.exists():
                    try:
                        content = skill_file.read_text(encoding="utf-8")
                        parsed = parse_skill_md(content, str(skill_file))
                        if parsed and parsed.name == skill_name:
                            # Verify tenant_id and agent_id match if provided
                            if tenant_id is not None and parsed.tenant_id != tenant_id:
                                continue
                            if agent_id is not None and parsed.agent_id != agent_id:
                                continue
                            found_dirs.append(subdir)
                            if skill_found is None:
                                skill_found = parsed
                    except Exception:
                        continue
            
            if found_dirs:
                skill_dir = found_dirs[0]
    
    # Verify SKILL.md exists
    if skill_dir:
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            return None, None
    
    return skill_dir, skill_found


async def _replace_skills_with_reindex(new_skills: list[Skill]) -> int:
    """Replace existing skills with updated versions and reindex search engine."""
    if search_engine is None:
        raise RuntimeError("Search engine not initialized")

    global reload_lock
    if reload_lock is None:
        reload_lock = asyncio.Lock()

    async with reload_lock:
        if loading_state_global:
            with loading_state_global._lock:
                loading_state_global.is_complete = False
                loading_state_global.loaded_skills = 0
                loading_state_global.total_skills = 0

        def _rebuild_index() -> int:
            with search_engine._lock:  # type: ignore[attr-defined]
                existing_skills = list(search_engine.skills)

                # Use composite key (name, tenant_id, agent_id) for uniqueness
                skills_by_key: dict[tuple[str, str | None, str | None], Skill] = {}
                for skill in existing_skills:
                    key = (skill.name, skill.tenant_id, skill.agent_id)
                    skills_by_key[key] = skill
                
                for skill in new_skills:
                    key = (skill.name, skill.tenant_id, skill.agent_id)
                    skills_by_key[key] = skill

                combined_skills = list(skills_by_key.values())
                # Call index_skills implementation directly while lock is held
                # to avoid releasing lock between getting existing skills and indexing
                if not combined_skills:
                    logger.warning("No skills to index")
                    search_engine.skills = []
                    search_engine.embeddings = None
                    return 0

                logger.info(f"Indexing {len(combined_skills)} skills...")
                search_engine.skills = combined_skills

                # Generate embeddings from skill descriptions
                descriptions = [skill.description for skill in combined_skills]
                model = search_engine._ensure_model_loaded()
                search_engine.embeddings = model.encode(descriptions, convert_to_numpy=True)

                logger.info(f"Successfully indexed {len(combined_skills)} skills")
                return len(combined_skills)

        try:
            total_skills = await asyncio.to_thread(_rebuild_index)
        except Exception as exc:  # pragma: no cover - defensive
            if loading_state_global:
                loading_state_global.add_error(str(exc))
                with loading_state_global._lock:
                    loading_state_global.is_complete = True
            raise

        if loading_state_global:
            with loading_state_global._lock:
                loading_state_global.total_skills = total_skills
                loading_state_global.loaded_skills = total_skills
                loading_state_global.is_complete = True

        return total_skills


async def upload_skill_archive(request):
    """Handle skill ZIP uploads and dynamically add/update skills."""
    if request.method != "POST":  # pragma: no cover - Starlette enforces methods
        return JSONResponse({"detail": "Method not allowed"}, status_code=405)

    if search_engine is None or config_global is None or loading_state_global is None:
        return JSONResponse(
            {"detail": "Backend not fully initialized"}, status_code=503
        )

    if not loading_state_global.is_complete:
        return JSONResponse(
            {"detail": "Skill loading in progress. Try again shortly."},
            status_code=409,
        )

    form = await request.form()
    upload = form.get("file")
    
    # Get optional agent_id and tenant_id from form
    agent_id = form.get("agent_id")
    tenant_id = form.get("tenant_id")

    if upload is None:
        return JSONResponse({"detail": "No file uploaded under field 'file'"}, 400)

    filename = getattr(upload, "filename", None)
    if not filename:
        return JSONResponse({"detail": "Uploaded file is missing a filename"}, 400)

    if not filename.lower().endswith(".zip"):
        return JSONResponse({"detail": "Only ZIP archives are supported"}, 400)

    local_root = _get_primary_local_skill_root()
    if local_root is None:
        return JSONResponse(
            {"detail": "No local skill source configured for uploads"}, 500
        )

    temp_dir = Path(tempfile.mkdtemp(prefix="skill_upload_"))
    archive_path = temp_dir / filename
    extract_dir = temp_dir / "extracted"
    extract_dir.mkdir(parents=True, exist_ok=True)

    try:
        data = await upload.read()
        await upload.close()
        if not data:
            return JSONResponse({"detail": "Uploaded file is empty"}, 400)

        archive_path.write_bytes(data)

        with zipfile.ZipFile(archive_path) as zf:
            _safe_extract_zip(zf, extract_dir)

        skill_files = list(extract_dir.rglob("SKILL.md"))
        if not skill_files:
            return JSONResponse(
                {"detail": "No SKILL.md found in the archive"}, status_code=400
            )

        added_skills: dict[str, Skill] = {}
        for skill_file in skill_files:
            try:
                content = skill_file.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                logger.warning("Failed to decode SKILL.md at %s", skill_file)
                continue

            parsed = parse_skill_md(content, str(skill_file))
            if not parsed:
                logger.warning("Skipping invalid SKILL.md at %s", skill_file)
                continue

            # Build destination directory path with tenant_id only
            # Skills are stored at tenant level, agents select from tenant skills
            skill_slug = _slugify(parsed.name or skill_file.parent.name)
            if tenant_id:
                # Store in tenant subdirectory
                dest_dir = local_root / _slugify(tenant_id) / skill_slug
            else:
                # Public skill (no tenant) - store at root level
                dest_dir = local_root / skill_slug
            
            if dest_dir.exists():
                logger.info(f"Replacing existing skill at {dest_dir}")
                shutil.rmtree(dest_dir)
            dest_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(skill_file.parent, dest_dir)
            logger.info(f"Skill saved to persistent storage: {dest_dir} (will be loaded on server restart)")

            loaded_skills = load_from_local(str(dest_dir), config_global)
            for skill in loaded_skills:
                # Set agent_id and tenant_id if provided
                if agent_id:
                    skill.agent_id = agent_id
                if tenant_id:
                    skill.tenant_id = tenant_id
                    # If tenant_id is set, this is a tenant-scoped skill
                    skill.scope = "tenant"
                # Use composite key for uniqueness: (name, tenant_id, agent_id)
                skill_key = (skill.name, tenant_id or None, agent_id or None)
                added_skills[skill_key] = skill
                logger.info(
                    f"Loaded skill '{skill.name}' from persistent storage: {dest_dir} "
                    f"(scope={skill.scope}, agent_id={agent_id}, tenant_id={tenant_id})"
                )

        if not added_skills:
            return JSONResponse(
                {"detail": "No valid skills could be loaded from the archive"}, 400
            )

        try:
            total_count = await _replace_skills_with_reindex(list(added_skills.values()))
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Failed to reindex skills after upload")
            return JSONResponse(
                {"detail": f"Failed to update skill index: {exc}"}, status_code=500
            )

        skill_names = [key[0] if isinstance(key, tuple) else key for key in added_skills.keys()]
        logger.info(
            f"Successfully uploaded and indexed {len(skill_names)} skill(s): {skill_names}. "
            f"Skills are saved to {local_root} and will persist across server restarts."
        )
        return JSONResponse(
            {
                "status": "ok",
                "skills_added": skill_names,
                "total_skills": total_count,
                "persistent_storage_path": str(local_root),
                "message": f"Skills saved to {local_root} and will be available after server restart",
            }
        )

    except zipfile.BadZipFile:
        return JSONResponse({"detail": "Uploaded file is not a valid ZIP archive"}, 400)
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, 400)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _parse_github_url(url: str) -> tuple[str, str, str] | None:
    """Parse GitHub URL to extract owner, repo, and branch.
    
    Parameters
    ----------
    url : str
        GitHub repository URL. Can be:
        - Base repo URL: https://github.com/owner/repo
        - URL with branch: https://github.com/owner/repo/tree/branch
        - URL with branch and subpath: https://github.com/owner/repo/tree/branch/subpath
    
    Returns
    -------
    tuple[str, str, str] | None
        (owner, repo, branch) or None if invalid.
    """
    try:
        parsed = urlparse(url)
        path_parts = parsed.path.strip("/").split("/")
        
        if len(path_parts) < 2:
            return None
        
        owner = path_parts[0]
        repo = path_parts[1]
        branch = "main"  # Default branch
        
        # Check for /tree/{branch}/ format
        if len(path_parts) > 3 and path_parts[2] == "tree":
            branch = path_parts[3]
        
        return owner, repo, branch
    except Exception as e:
        logger.error(f"Failed to parse GitHub URL {url}: {e}")
        return None


async def upload_skill_from_github(request):
    """Handle skill registration from GitHub repository URL.
    
    Downloads the repository as a ZIP archive, extracts it, and registers
    any SKILL.md files found as custom skills.
    """
    if request.method != "POST":  # pragma: no cover - Starlette enforces methods
        return JSONResponse({"detail": "Method not allowed"}, status_code=405)
    
    if search_engine is None or config_global is None or loading_state_global is None:
        return JSONResponse(
            {"detail": "Backend not fully initialized"}, status_code=503
        )
    
    if not loading_state_global.is_complete:
        return JSONResponse(
            {"detail": "Skill loading in progress. Try again shortly."},
            status_code=409,
        )
    
    # Get GitHub URL from request body
    try:
        body = await request.json()
    except Exception as e:
        return JSONResponse(
            {"detail": f"Invalid JSON in request body: {e}"}, status_code=400
        )
    
    github_url = body.get("url")
    if not github_url:
        return JSONResponse(
            {"detail": "Missing required field 'url' in request body"}, status_code=400
        )
    
    # Get optional agent_id and tenant_id from request body
    agent_id = body.get("agent_id")
    tenant_id = body.get("tenant_id")
    
    if not isinstance(github_url, str) or "github.com" not in github_url:
        return JSONResponse(
            {"detail": "Invalid GitHub URL. Must be a valid GitHub repository URL."},
            status_code=400,
        )
    
    # Parse GitHub URL
    parsed = _parse_github_url(github_url)
    if not parsed:
        return JSONResponse(
            {"detail": f"Failed to parse GitHub URL: {github_url}"}, status_code=400
        )
    
    owner, repo, branch = parsed
    
    local_root = _get_primary_local_skill_root()
    if local_root is None:
        return JSONResponse(
            {"detail": "No local skill source configured for uploads"}, status_code=500
        )
    
    temp_dir = Path(tempfile.mkdtemp(prefix="skill_github_"))
    archive_path = temp_dir / f"{repo}-{branch}.zip"
    extract_dir = temp_dir / "extracted"
    extract_dir.mkdir(parents=True, exist_ok=True)
    
    try:
        # Download repository as ZIP from GitHub
        # GitHub provides ZIP downloads at: https://github.com/owner/repo/archive/refs/heads/branch.zip
        zip_url = f"https://github.com/{owner}/{repo}/archive/refs/heads/{branch}.zip"
        
        logger.info(f"Downloading GitHub repository {owner}/{repo} (branch: {branch}) from {zip_url}")
        
        # Use httpx to download the ZIP file
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            try:
                response = await client.get(zip_url)
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    # Try with 'main' branch if the specified branch doesn't exist
                    if branch != "main":
                        logger.warning(f"Branch '{branch}' not found, trying 'main' branch")
                        zip_url = f"https://github.com/{owner}/{repo}/archive/refs/heads/main.zip"
                        response = await client.get(zip_url)
                        response.raise_for_status()
                        branch = "main"
                    else:
                        return JSONResponse(
                            {"detail": f"Repository or branch not found: {github_url}"},
                            status_code=404,
                        )
                else:
                    return JSONResponse(
                        {"detail": f"Failed to download repository: {e}"},
                        status_code=500,
                    )
            except Exception as e:
                return JSONResponse(
                    {"detail": f"Failed to download repository: {e}"}, status_code=500
                )
            
            # Save ZIP file
            archive_path.write_bytes(response.content)
            logger.info(f"Downloaded {len(response.content)} bytes from GitHub")
        
        # Extract ZIP file
        try:
            with zipfile.ZipFile(archive_path) as zf:
                _safe_extract_zip(zf, extract_dir)
        except zipfile.BadZipFile:
            return JSONResponse(
                {"detail": "Downloaded file is not a valid ZIP archive"}, status_code=400
            )
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        
        # GitHub ZIP archives have a top-level directory named {repo}-{branch}
        # Find the actual extracted directory
        extracted_dirs = [d for d in extract_dir.iterdir() if d.is_dir()]
        if not extracted_dirs:
            return JSONResponse(
                {"detail": "No directories found in downloaded archive"}, status_code=400
            )
        
        # Use the first directory (should be the repo-branch directory)
        repo_dir = extracted_dirs[0]
        
        # Find all SKILL.md files
        skill_files = list(repo_dir.rglob("SKILL.md"))
        if not skill_files:
            return JSONResponse(
                {"detail": "No SKILL.md found in the repository"}, status_code=400
            )
        
        added_skills: dict[str, Skill] = {}
        updated_skills: list[str] = []
        new_skills: list[str] = []
        
        for skill_file in skill_files:
            try:
                content = skill_file.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                logger.warning("Failed to decode SKILL.md at %s", skill_file)
                continue
            
            parsed = parse_skill_md(content, str(skill_file))
            if not parsed:
                logger.warning("Skipping invalid SKILL.md at %s", skill_file)
                continue
            
            # Build destination directory path with tenant_id only
            # Skills are stored at tenant level, agents select from tenant skills
            skill_slug = _slugify(parsed.name or skill_file.parent.name)
            skill_name = parsed.name or skill_file.parent.name
            if tenant_id:
                # Store in tenant subdirectory
                dest_dir = local_root / _slugify(tenant_id) / skill_slug
            else:
                # Public skill (no tenant) - store at root level
                dest_dir = local_root / skill_slug
            
            is_update = dest_dir.exists()
            
            if is_update:
                logger.info(f"Updating existing skill '{skill_name}' at {dest_dir}")
                shutil.rmtree(dest_dir)
                updated_skills.append(skill_name)
            else:
                logger.info(f"Adding new skill '{skill_name}' at {dest_dir}")
                new_skills.append(skill_name)
            
            # Copy the skill directory (parent of SKILL.md)
            dest_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(skill_file.parent, dest_dir)
            logger.info(
                f"Skill saved to persistent storage: {dest_dir} (will be loaded on server restart)"
            )
            
            loaded_skills = load_from_local(str(dest_dir), config_global)
            for skill in loaded_skills:
                # Set agent_id and tenant_id if provided
                if agent_id:
                    skill.agent_id = agent_id
                if tenant_id:
                    skill.tenant_id = tenant_id
                    # If tenant_id is set, this is a tenant-scoped skill
                    skill.scope = "tenant"
                # Use composite key for uniqueness: (name, tenant_id, agent_id)
                skill_key = (skill.name, tenant_id or None, agent_id or None)
                added_skills[skill_key] = skill
                logger.info(
                    f"Loaded skill '{skill.name}' from persistent storage: {dest_dir} "
                    f"(scope={skill.scope}, agent_id={agent_id}, tenant_id={tenant_id})"
                )
        
        if not added_skills:
            return JSONResponse(
                {"detail": "No valid skills could be loaded from the repository"},
                status_code=400,
            )
        
        try:
            total_count = await _replace_skills_with_reindex(list(added_skills.values()))
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Failed to reindex skills after GitHub upload")
            return JSONResponse(
                {"detail": f"Failed to update skill index: {exc}"}, status_code=500
            )
        
        skill_names = [key[0] if isinstance(key, tuple) else key for key in added_skills.keys()]
        logger.info(
            f"Successfully uploaded and indexed {len(skill_names)} skill(s) from GitHub "
            f"repository {owner}/{repo}@{branch}: {skill_names}. "
            f"New: {len(new_skills)}, Updated: {len(updated_skills)}. "
            f"Skills are saved to {local_root} and will persist across server restarts."
        )
        return JSONResponse(
            {
                "status": "ok",
                "repository": f"{owner}/{repo}",
                "branch": branch,
                "github_url": github_url,
                "skills_added": skill_names,
                "new_skills": new_skills,
                "updated_skills": updated_skills,
                "total_skills": total_count,
                "persistent_storage_path": str(local_root),
                "message": f"Skills saved to {local_root} and will be available after server restart. "
                          f"To update, call this endpoint again with the same URL.",
            }
        )
    
    except Exception as exc:
        logger.exception(f"Error processing GitHub repository {github_url}")
        return JSONResponse(
            {"detail": f"Error processing repository: {exc}"}, status_code=500
        )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def register_mcp_tools(default_top_k: int = 3, max_content_chars: int | None = None):
    """Register MCP tools using FastMCP decorators."""
    
    # Import handle functions from mcp_handlers
    from .mcp_handlers import (
        handle_search_skills,
        handle_read_skill_document,
        handle_list_skills,
    )
    
    @mcp.tool(
        name="find_helpful_skills",
        title="Find the most helpful skill for any task",
        description=(
            "Always call this tool FIRST whenever the question requires any domain-specific knowledge "
            "beyond common sense or simple recall. Use it at task start, regardless of the task and whether "
            "you are sure about the task, It performs semantic search over a curated library of proven skills "
            "and returns ranked candidates with step-by-step guidance and best practices. Do this before any "
            "searches, coding, or any other actions as this will inform you about the best approach to take."
        )
    )
    async def find_helpful_skills(
        task_description: str,
        tenant_id: str,
        allowed_skill_names: list[str] | None = None,
        top_k: int = default_top_k,
        list_documents: bool = True,
    ) -> list[TextContent]:
        """Search for relevant skills.
        
        Note: allowed_skill_names should be provided by the API server
        based on the users.skills column in the database. The skill engine
        does not perform authorization checks and trusts the provided values.
        """
        return await handle_search_skills(
            {
                "task_description": task_description,
                "top_k": top_k,
                "list_documents": list_documents,
                "tenant_id": tenant_id,
                "allowed_skill_names": allowed_skill_names or [],
            },
            search_engine,
            loading_state_global,
            default_top_k,
            max_content_chars,
        )
    
    @mcp.tool(
        name="read_skill_document",
        title="Open skill documents and assets",
        description=(
            "Use after finding a relevant skill to retrieve specific documents (scripts, references, assets). "
            "Supports pattern matching (e.g., 'scripts/*.py') to fetch multiple files. Returns text content or URLs "
            "and never executes code. Prefer pulling only the files you need to complete the current step."
        )
    )
    async def read_skill_document(
        skill_name: str,
        document_path: str | None = None,
        include_base64: bool = False
    ) -> list[TextContent]:
        """Read a document from a skill."""
        args = {"skill_name": skill_name, "include_base64": include_base64}
        if document_path is not None:
            args["document_path"] = document_path
        return await handle_read_skill_document(args, search_engine)
    
    @mcp.tool(
        name="list_skills",
        title="List available skills",
        description=(
            "Returns the full inventory of loaded skills (names, descriptions, sources, document counts) "
            "for exploration or debugging. For task-driven work, prefer calling 'find_helpful_skills' first "
            "to locate the most relevant option before reading documents."
        )
    )
    async def list_skills() -> list[TextContent]:
        """List all loaded skills."""
        return await handle_list_skills({}, search_engine, loading_state_global)
    
    @mcp.tool(
        name="delete_skill",
        title="Delete an entire skill",
        description=(
            "Delete a skill and all its files from local storage. This removes the skill directory "
            "and removes it from the search index. Use with caution as this action cannot be undone. "
            "Requires tenant_id and agent_id if the skill is tenant/agent-scoped."
        )
    )
    async def delete_skill(
        skill_name: str,
        tenant_id: str | None = None,
        agent_id: str | None = None,
    ) -> list[TextContent]:
        """Delete an entire skill (directory and all files)."""
        if config_global is None:
            return [TextContent(
                type="text",
                text=f"Error: Backend not fully initialized"
            )]
        
        # Find the skill directory
        skill_dir, _ = _find_skill_directory(skill_name, tenant_id, agent_id)
        if skill_dir is None:
            return [TextContent(
                type="text",
                text=f"Error: Skill '{skill_name}' not found in local storage"
            )]
        
        # Verify the skill directory is within the local root (security check)
        local_root = _get_primary_local_skill_root()
        if local_root is None:
            return [TextContent(
                type="text",
                text="Error: No local skill source configured"
            )]
        
        try:
            # Ensure the skill directory is within local_root
            skill_dir_resolved = skill_dir.resolve()
            local_root_resolved = local_root.resolve()
            if not str(skill_dir_resolved).startswith(str(local_root_resolved)):
                return [TextContent(
                    type="text",
                    text="Error: Invalid skill path: path traversal detected"
                )]
        except Exception as e:
            return [TextContent(
                type="text",
                text=f"Error: Invalid skill path: {e}"
            )]
        
        if not skill_dir.exists():
            return [TextContent(
                type="text",
                text=f"Error: Skill directory for '{skill_name}' does not exist"
            )]
        
        try:
            # Remove skill from search engine index first
            try:
                await _remove_skill_from_index(skill_name, tenant_id, agent_id)
                logger.info(f"Removed skill '{skill_name}' from search index (tenant_id={tenant_id}, agent_id={agent_id})")
            except Exception as e:
                logger.warning(f"Failed to remove skill '{skill_name}' from index: {e}")
                # Continue with directory deletion even if index removal fails
            
            # Delete the entire skill directory
            shutil.rmtree(skill_dir)
            logger.info(f"Deleted skill directory '{skill_dir}' for skill '{skill_name}'")
            
            return [TextContent(
                type="text",
                text=f"Successfully deleted skill '{skill_name}' and all its files."
            )]
        except Exception as exc:
            logger.exception(f"Failed to delete skill '{skill_name}'")
            return [TextContent(
                type="text",
                text=f"Error: Failed to delete skill: {exc}"
            )]
    
    @mcp.tool(
        name="delete_skill_file",
        title="Delete a file from a skill",
        description=(
            "Delete a specific file from a skill. Cannot delete SKILL.md file. "
            "The skill will be automatically reloaded after file deletion to update the search index."
        )
    )
    async def delete_skill_file(
        skill_name: str,
        file_path: str,
        tenant_id: str | None = None,
        agent_id: str | None = None,
    ) -> list[TextContent]:
        """Delete a specific file from a skill."""
        if config_global is None:
            return [TextContent(
                type="text",
                text="Error: Backend not fully initialized"
            )]
        
        # Prevent deletion of SKILL.md
        if file_path == "SKILL.md" or file_path.endswith("/SKILL.md"):
            return [TextContent(
                type="text",
                text="Error: Cannot delete SKILL.md file"
            )]
        
        # Find the skill directory
        skill_dir, _ = _find_skill_directory(skill_name, tenant_id, agent_id)
        if skill_dir is None:
            return [TextContent(
                type="text",
                text=f"Error: Skill '{skill_name}' not found in local storage"
            )]
        
        # Build full file path
        try:
            # Prevent path traversal
            full_file_path = skill_dir / file_path
            # Ensure the file is within skill_dir
            full_file_path = full_file_path.resolve()
            skill_dir_resolved = skill_dir.resolve()
            if not str(full_file_path).startswith(str(skill_dir_resolved)):
                return [TextContent(
                    type="text",
                    text="Error: Invalid file path: path traversal detected"
                )]
        except Exception as e:
            return [TextContent(
                type="text",
                text=f"Error: Invalid file path: {e}"
            )]
        
        if not full_file_path.exists():
            return [TextContent(
                type="text",
                text=f"Error: File '{file_path}' not found in skill '{skill_name}'"
            )]
        
        if not full_file_path.is_file():
            return [TextContent(
                type="text",
                text=f"Error: Path '{file_path}' is not a file"
            )]
        
        try:
            # Delete the file
            full_file_path.unlink()
            
            # Reload the skill to update search index
            if config_global:
                try:
                    loaded_skills = load_from_local(str(skill_dir), config_global)
                    if loaded_skills:
                        await _replace_skills_with_reindex(loaded_skills)
                        logger.info(f"Reloaded skill '{skill_name}' after file deletion")
                except Exception as e:
                    logger.warning(f"Failed to reload skill after file deletion: {e}")
            
            logger.info(f"Deleted file '{file_path}' from skill '{skill_name}'")
            return [TextContent(
                type="text",
                text=f"Successfully deleted file '{file_path}' from skill '{skill_name}'."
            )]
        except Exception as exc:
            logger.exception(f"Failed to delete file '{file_path}' from skill '{skill_name}'")
            return [TextContent(
                type="text",
                text=f"Error: Failed to delete file: {exc}"
            )]
    
    @mcp.tool(
        name="update_skill_file",
        title="Update a file in a skill",
        description=(
            "Update or create a file in a skill. Can update text files directly or binary files via base64 encoding. "
            "The skill will be automatically reloaded after file update to update the search index."
        )
    )
    async def update_skill_file(
        skill_name: str,
        file_path: str,
        content: str | None = None,
        content_base64: str | None = None,
        tenant_id: str | None = None,
        agent_id: str | None = None,
    ) -> list[TextContent]:
        """Update a specific file in a skill."""
        if config_global is None:
            return [TextContent(
                type="text",
                text="Error: Backend not fully initialized"
            )]
        
        if content is None and content_base64 is None:
            return [TextContent(
                type="text",
                text="Error: Missing 'content' or 'content_base64' parameter"
            )]
        
        # Find the skill directory
        skill_dir, _ = _find_skill_directory(skill_name, tenant_id, agent_id)
        if skill_dir is None:
            return [TextContent(
                type="text",
                text=f"Error: Skill '{skill_name}' not found in local storage"
            )]
        
        # Build full file path
        try:
            # Prevent path traversal
            full_file_path = skill_dir / file_path
            # Ensure the file is within skill_dir
            full_file_path = full_file_path.resolve()
            skill_dir_resolved = skill_dir.resolve()
            if not str(full_file_path).startswith(str(skill_dir_resolved)):
                return [TextContent(
                    type="text",
                    text="Error: Invalid file path: path traversal detected"
                )]
        except Exception as e:
            return [TextContent(
                type="text",
                text=f"Error: Invalid file path: {e}"
            )]
        
        try:
            # Create parent directories if needed
            full_file_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Write file content
            if content is not None:
                # Text content
                full_file_path.write_text(content, encoding="utf-8")
            else:
                # Binary content from base64
                binary_content = base64.b64decode(content_base64)
                full_file_path.write_bytes(binary_content)
            
            # Reload the skill to update search index
            if config_global:
                try:
                    loaded_skills = load_from_local(str(skill_dir), config_global)
                    if loaded_skills:
                        await _replace_skills_with_reindex(loaded_skills)
                        logger.info(f"Reloaded skill '{skill_name}' after file update")
                except Exception as e:
                    logger.warning(f"Failed to reload skill after file update: {e}")
            
            stat = full_file_path.stat()
            logger.info(f"Updated file '{file_path}' in skill '{skill_name}'")
            return [TextContent(
                type="text",
                text=f"Successfully updated file '{file_path}' in skill '{skill_name}'. Size: {stat.st_size} bytes."
            )]
        except Exception as exc:
            logger.exception(f"Failed to update file '{file_path}' in skill '{skill_name}'")
            return [TextContent(
                type="text",
                text=f"Error: Failed to update file: {exc}"
            )]


async def check_skill(request):
    """Check if a skill exists by name.
    
    First checks the search engine index, then falls back to local storage
    to handle cases where skills exist in volume but weren't loaded due to
    network issues during initialization.
    
    Filters skills by agent_id and tenant_id if provided:
    - Public skills (no agent_id/tenant_id) are always included
    - Uploaded skills must match both agent_id and tenant_id
    """
    if search_engine is None:
        return JSONResponse(
            {"detail": "Backend not fully initialized"}, status_code=503
        )

    # Get skill name from query parameter
    skill_name = request.query_params.get("name")
    if not skill_name:
        return JSONResponse(
            {"detail": "Missing required query parameter: name"}, status_code=400
        )

    # Get optional agent_id and tenant_id from query parameters
    agent_id = request.query_params.get("agent_id")
    tenant_id = request.query_params.get("tenant_id")

    # Search for the skill by name in search engine with filtering
    skill_found = None
    with search_engine._lock:
        for skill in search_engine.skills:
            if skill.name != skill_name:
                continue
            
            # Check if this is a public skill (no agent_id or tenant_id)
            is_public_skill = skill.agent_id is None and skill.tenant_id is None
            
            # Public skills are always included (no filtering)
            if is_public_skill:
                skill_found = skill
                break
            
            # For uploaded skills (non-public), they must match the provided filters
            # Both agent_id and tenant_id must match for uploaded skills
            if skill.agent_id == agent_id and skill.tenant_id == tenant_id:
                skill_found = skill
                break

    # If not found in search engine, check local storage
    if not skill_found:
        logger.info(
            f"Skill '{skill_name}' not found in index (agent_id={agent_id}, tenant_id={tenant_id}), "
            "checking local storage..."
        )
        skill_dir, skill_from_storage = _find_skill_directory(skill_name, tenant_id, agent_id)
        
        if skill_from_storage and skill_dir:
            # Skill exists in local storage but not in search engine
            # Load it and add to search engine
            logger.info(f"Found skill '{skill_name}' in local storage at {skill_dir}, adding to search engine")
            try:
                if config_global:
                    loaded_skills = load_from_local(str(skill_dir), config_global)
                    if loaded_skills:
                        # Set tenant_id and agent_id if they match the filters
                        for loaded_skill in loaded_skills:
                            if tenant_id and loaded_skill.tenant_id != tenant_id:
                                continue
                            if agent_id and loaded_skill.agent_id != agent_id:
                                continue
                            # Add to search engine
                            search_engine.add_skills([loaded_skill])
                            skill_found = loaded_skill
                            logger.info(
                                f"Successfully loaded and indexed skill '{skill_name}' from local storage "
                                f"(agent_id={agent_id}, tenant_id={tenant_id})"
                            )
                            break
            except Exception as e:
                logger.warning(f"Failed to load skill '{skill_name}' from local storage: {e}")

    if skill_found:
        skill_info = {
            "name": skill_found.name,
            "description": skill_found.description,
            "source": skill_found.source,
            "document_count": len(skill_found.documents),
            "exists": True,
        }
        logger.info(
            f"Skill '{skill_name}' found (agent_id={agent_id}, tenant_id={tenant_id})"
        )
        return JSONResponse(skill_info, status_code=200)
    else:
        logger.info(
            f"Skill '{skill_name}' not found in index or local storage "
            f"(agent_id={agent_id}, tenant_id={tenant_id})"
        )
        return JSONResponse(
            {
                "name": skill_name,
                "exists": False,
                "detail": f"Skill '{skill_name}' not found. Available skills: {len(search_engine.skills)} total",
            },
            status_code=404,
        )


async def download_skill_archive(request):
    """Download an uploaded skill as a ZIP archive."""
    if request.method != "GET":  # pragma: no cover - Starlette enforces methods
        return JSONResponse({"detail": "Method not allowed"}, status_code=405)

    if config_global is None:
        return JSONResponse(
            {"detail": "Backend not fully initialized"}, status_code=503
        )

    # Get skill name from query parameter
    skill_name = request.query_params.get("name")
    if not skill_name:
        return JSONResponse(
            {"detail": "Missing required query parameter: name"}, status_code=400
        )

    # Get optional agent_id and tenant_id from query parameters
    agent_id = request.query_params.get("agent_id")
    tenant_id = request.query_params.get("tenant_id")

    local_root = _get_primary_local_skill_root()
    if local_root is None:
        return JSONResponse(
            {"detail": "No local skill source configured"}, status_code=500
        )

    # Find the skill directory
    skill_dir, skill_found = _find_skill_directory(skill_name, tenant_id, agent_id)
    if skill_dir is None:
        return JSONResponse(
            {"detail": f"Skill '{skill_name}' not found in local storage"}, 
            status_code=404
        )

    # Create ZIP archive in memory
    zip_buffer = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    zip_path = Path(zip_buffer.name)
    
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            # Add all files in the skill directory
            for file_path in skill_dir.rglob("*"):
                if file_path.is_file():
                    # Get relative path from skill_dir
                    arcname = file_path.relative_to(skill_dir)
                    zf.write(file_path, arcname)
                    logger.debug(f"Added {arcname} to ZIP archive")

        # Read ZIP file content
        zip_data = zip_path.read_bytes()
        
        # Get the skill name for the filename
        if skill_found:
            zip_filename = f"{_slugify(skill_found.name)}.zip"
        else:
            # Use directory name as fallback
            zip_filename = f"{skill_dir.name}.zip"

        logger.info(f"Downloading skill '{skill_name}' as {zip_filename}")
        
        # Return ZIP file as response
        return Response(
            content=zip_data,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{zip_filename}"',
                "Content-Length": str(len(zip_data)),
            },
        )
    except Exception as exc:
        logger.exception(f"Failed to create ZIP archive for skill '{skill_name}'")
        return JSONResponse(
            {"detail": f"Failed to create archive: {exc}"}, status_code=500
        )
    finally:
        # Clean up temporary file
        zip_path.unlink(missing_ok=True)


async def list_uploaded_skills(request):
    """List all skills uploaded to local storage, optionally filtered by tenant_id."""
    if request.method != "GET":  # pragma: no cover - Starlette enforces methods
        return JSONResponse({"detail": "Method not allowed"}, status_code=405)

    if config_global is None:
        return JSONResponse(
            {"detail": "Backend not fully initialized"}, status_code=503
        )

    local_root = _get_primary_local_skill_root()
    if local_root is None:
        return JSONResponse(
            {"detail": "No local skill source configured"}, status_code=500
        )

    if not local_root.exists():
        return JSONResponse(
            {"skills": [], "count": 0, "storage_path": str(local_root)}
        )

    # Get optional tenant_id from query parameters
    tenant_id = request.query_params.get("tenant_id")
    
    uploaded_skills = []
    
    # Recursively scan for all SKILL.md files
    skill_files = list(local_root.rglob("SKILL.md"))
    
    for skill_file in skill_files:
        try:
            # Get the skill directory (parent of SKILL.md)
            skill_dir = skill_file.parent
            
            # Determine tenant_id from path
            # Path structure: local_root / tenant_id / skill_slug / SKILL.md
            # or: local_root / skill_slug / SKILL.md (public skills)
            try:
                relative_path = skill_dir.relative_to(local_root)
                path_parts = relative_path.parts
                
                skill_tenant_id = None
                # If path has 2 parts (tenant_id/skill_slug), first part is tenant_id
                # If path has 1 part (skill_slug), it's a public skill (no tenant_id)
                if len(path_parts) == 2:
                    skill_tenant_id = path_parts[0]
                elif len(path_parts) == 1:
                    skill_tenant_id = None  # Public skill
                # If path has more than 2 parts, it might be nested incorrectly
                # but we'll still try to extract tenant_id from first part
                elif len(path_parts) > 2:
                    # Check if first part looks like a tenant directory
                    potential_tenant_dir = local_root / path_parts[0]
                    if potential_tenant_dir.is_dir():
                        skill_tenant_id = path_parts[0]
            except ValueError:
                # skill_dir is not relative to local_root, skip
                continue
            
            # Filter by tenant_id if provided
            if tenant_id is not None:
                if skill_tenant_id != tenant_id:
                    continue
            
            content = skill_file.read_text(encoding="utf-8")
            parsed = parse_skill_md(content, str(skill_file))
            if parsed:
                # Count files in the directory
                file_count = sum(1 for f in skill_dir.rglob("*") if f.is_file())
                
                uploaded_skills.append({
                    "name": parsed.name,
                    "description": parsed.description,
                    "directory": skill_dir.name,
                    "file_count": file_count,
                    "path": str(skill_dir),
                    "tenant_id": skill_tenant_id,
                })
        except Exception as e:
            logger.warning(f"Error reading skill from {skill_file}: {e}")
            continue

    logger.info(f"Found {len(uploaded_skills)} uploaded skills in local storage (tenant_id={tenant_id})")
    return JSONResponse(
        {
            "skills": uploaded_skills,
            "count": len(uploaded_skills),
            "storage_path": str(local_root),
            "tenant_id": tenant_id,
        }
    )


def _is_builtin_skill(skill: Skill, local_root: Path | None) -> bool:
    """Return True if skill is built-in (not from primary local upload storage)."""
    if not skill.source:
        return True
    source_str = str(skill.source)
    if "github.com" in source_str:
        return True
    if local_root is None:
        return True
    try:
        source_path = Path(skill.source).resolve()
        return not source_path.is_relative_to(local_root.resolve())
    except (ValueError, OSError):
        return True


async def list_builtin_skills(request):
    """List all built-in (non-uploaded) skills from the search index."""
    if request.method != "GET":  # pragma: no cover - Starlette enforces methods
        return JSONResponse({"detail": "Method not allowed"}, status_code=405)

    if config_global is None:
        return JSONResponse(
            {"detail": "Backend not fully initialized"}, status_code=503
        )
    if search_engine is None:
        return JSONResponse(
            {"detail": "Search engine not initialized"}, status_code=503
        )

    local_root = _get_primary_local_skill_root()
    builtin = [
        skill
        for skill in search_engine.skills
        if _is_builtin_skill(skill, local_root)
    ]

    # Format source as owner/repo for GitHub URLs
    def _format_source(source: str) -> str:
        if "github.com" in source:
            match = re.search(r"github\.com/([^/]+/[^/]+)", source)
            if match:
                return match.group(1)
        return source

    builtin_list = [
        {
            "name": skill.name,
            "description": skill.description,
            "source": _format_source(skill.source or ""),
            "document_count": len(skill.documents),
        }
        for skill in builtin
    ]

    logger.info(f"Found {len(builtin_list)} built-in skills")
    return JSONResponse(
        {
            "skills": builtin_list,
            "count": len(builtin_list),
        }
    )


async def list_skill_files(request):
    """List all files in a skill directory."""
    if request.method != "GET":  # pragma: no cover - Starlette enforces methods
        return JSONResponse({"detail": "Method not allowed"}, status_code=405)

    if config_global is None:
        return JSONResponse(
            {"detail": "Backend not fully initialized"}, status_code=503
        )

    # Get skill name from path
    skill_name = request.path_params.get("skill_name")
    if not skill_name:
        return JSONResponse(
            {"detail": "Missing skill name in path"}, status_code=400
        )

    # Get optional agent_id and tenant_id from query parameters
    agent_id = request.query_params.get("agent_id")
    tenant_id = request.query_params.get("tenant_id")

    # Find the skill (uploaded or built-in)
    skill_dir, skill = _find_skill_directory(skill_name, tenant_id, agent_id)
    if skill_dir is None and skill is None:
        return JSONResponse(
            {"detail": f"Skill '{skill_name}' not found"}, status_code=404
        )

    files = []
    try:
        if skill_dir is not None:
            # Uploaded skill: list files from filesystem
            for file_path in skill_dir.rglob("*"):
                if file_path.is_file():
                    try:
                        rel_path = str(file_path.relative_to(skill_dir))
                        stat = file_path.stat()
                        files.append({
                            "path": rel_path,
                            "size": stat.st_size,
                            "modified": stat.st_mtime,
                        })
                    except Exception as e:
                        logger.warning(f"Error getting file info for {file_path}: {e}")
                        continue
        else:
            # Built-in skill: list SKILL.md + documents from skill index
            files.append({
                "path": "SKILL.md",
                "size": len(skill.content),
                "modified": 0,
            })
            for doc_path, doc_meta in skill.documents.items():
                files.append({
                    "path": doc_path,
                    "size": doc_meta.get("size", 0),
                    "modified": 0,
                })

        # Sort by path
        files.sort(key=lambda x: x["path"])
        
        logger.info(f"Listed {len(files)} files for skill '{skill_name}'")
        return JSONResponse({
            "skill_name": skill_name,
            "files": files,
            "count": len(files),
        })
    except Exception as exc:
        logger.exception(f"Failed to list files for skill '{skill_name}'")
        return JSONResponse(
            {"detail": f"Failed to list files: {exc}"}, status_code=500
        )


async def get_skill_file(request):
    """Get a specific file from a skill."""
    if request.method != "GET":  # pragma: no cover - Starlette enforces methods
        return JSONResponse({"detail": "Method not allowed"}, status_code=405)

    if config_global is None:
        return JSONResponse(
            {"detail": "Backend not fully initialized"}, status_code=503
        )

    # Get skill name and file path from path
    skill_name = request.path_params.get("skill_name")
    file_path_encoded = request.path_params.get("file_path", "")
    
    if not skill_name:
        return JSONResponse(
            {"detail": "Missing skill name in path"}, status_code=400
        )
    
    if not file_path_encoded:
        return JSONResponse(
            {"detail": "Missing file path in path"}, status_code=400
        )

    # Decode URL-encoded file path
    try:
        file_path_str = unquote(file_path_encoded)
    except Exception as e:
        return JSONResponse(
            {"detail": f"Invalid file path encoding: {e}"}, status_code=400
        )

    # Find the skill (uploaded or built-in)
    skill_dir, skill = _find_skill_directory(skill_name)
    if skill_dir is None and skill is None:
        return JSONResponse(
            {"detail": f"Skill '{skill_name}' not found"}, status_code=404
        )

    # Built-in skill: get content from skill index
    if skill_dir is None and skill is not None:
        # Prevent path traversal
        if ".." in file_path_str or file_path_str.startswith("/"):
            return JSONResponse(
                {"detail": "Invalid file path: path traversal detected"},
                status_code=400
            )
        if file_path_str == "SKILL.md":
            text_content = skill.content
            logger.info(f"Retrieved file 'SKILL.md' from built-in skill '{skill_name}'")
            return JSONResponse({
                "skill_name": skill_name,
                "file_path": "SKILL.md",
                "type": "text",
                "size": len(text_content),
                "modified": 0,
                "content": text_content,
                "source": "builtin",
            })
        doc = skill.get_document(file_path_str)
        if doc is None:
            # Verify path exists in documents (prevent path traversal)
            if file_path_str not in skill.documents:
                return JSONResponse(
                    {"detail": f"File '{file_path_str}' not found in skill '{skill_name}'"},
                    status_code=404
                )
            return JSONResponse(
                {"detail": f"Failed to fetch file '{file_path_str}' from skill '{skill_name}'"},
                status_code=500
            )
        doc_type = doc.get("type")
        if doc_type == "text":
            logger.info(f"Retrieved file '{file_path_str}' from built-in skill '{skill_name}'")
            return JSONResponse({
                "skill_name": skill_name,
                "file_path": file_path_str,
                "type": "text",
                "size": doc.get("size", len(doc.get("content", ""))),
                "modified": 0,
                "content": doc.get("content", ""),
                "source": "builtin",
            })
        if doc_type == "image":
            if doc.get("size_exceeded"):
                return JSONResponse({
                    "skill_name": skill_name,
                    "file_path": file_path_str,
                    "type": "image",
                    "size": doc.get("size", 0),
                    "modified": 0,
                    "url": doc.get("url", ""),
                    "note": "Size exceeds limit, access via URL",
                    "source": "builtin",
                })
            return JSONResponse({
                "skill_name": skill_name,
                "file_path": file_path_str,
                "type": "binary",
                "size": doc.get("size", 0),
                "modified": 0,
                "content_base64": doc.get("content", ""),
                "source": "builtin",
            })
        return JSONResponse(
            {"detail": f"Unsupported file type for '{file_path_str}'"}, status_code=400
        )

    # Uploaded skill: read from filesystem
    try:
        # Prevent path traversal
        file_path = skill_dir / file_path_str
        # Ensure the file is within skill_dir
        file_path = file_path.resolve()
        skill_dir_resolved = skill_dir.resolve()
        if not str(file_path).startswith(str(skill_dir_resolved)):
            return JSONResponse(
                {"detail": "Invalid file path: path traversal detected"},
                status_code=400
            )
    except Exception as e:
        return JSONResponse(
            {"detail": f"Invalid file path: {e}"}, status_code=400
        )

    if not file_path.exists():
        return JSONResponse(
            {"detail": f"File '{file_path_str}' not found in skill '{skill_name}'"},
            status_code=404
        )

    if not file_path.is_file():
        return JSONResponse(
            {"detail": f"Path '{file_path_str}' is not a file"},
            status_code=400
        )

    try:
        # Read file content
        content = file_path.read_bytes()
        stat = file_path.stat()
        
        # Determine content type
        suffix = file_path.suffix.lower()
        if suffix in [".md", ".txt", ".py", ".json", ".yaml", ".yml", ".sh", ".r", ".xml"]:
            # Text file - return as text
            try:
                text_content = file_path.read_text(encoding="utf-8")
                logger.info(f"Retrieved file '{file_path_str}' from skill '{skill_name}'")
                return JSONResponse({
                    "skill_name": skill_name,
                    "file_path": file_path_str,
                    "type": "text",
                    "size": stat.st_size,
                    "modified": stat.st_mtime,
                    "content": text_content,
                })
            except UnicodeDecodeError:
                # Binary text file - return as base64
                content_b64 = base64.b64encode(content).decode("utf-8")
                return JSONResponse({
                    "skill_name": skill_name,
                    "file_path": file_path_str,
                    "type": "binary",
                    "size": stat.st_size,
                    "modified": stat.st_mtime,
                    "content_base64": content_b64,
                })
        else:
            # Binary file - return as base64
            content_b64 = base64.b64encode(content).decode("utf-8")
            logger.info(f"Retrieved binary file '{file_path_str}' from skill '{skill_name}'")
            return JSONResponse({
                "skill_name": skill_name,
                "file_path": file_path_str,
                "type": "binary",
                "size": stat.st_size,
                "modified": stat.st_mtime,
                "content_base64": content_b64,
            })
    except Exception as exc:
        logger.exception(f"Failed to read file '{file_path_str}' from skill '{skill_name}'")
        return JSONResponse(
            {"detail": f"Failed to read file: {exc}"}, status_code=500
        )


async def update_skill_file(request):
    """Update a specific file in a skill."""
    if request.method != "PUT":  # pragma: no cover - Starlette enforces methods
        return JSONResponse({"detail": "Method not allowed"}, status_code=405)

    if config_global is None:
        return JSONResponse(
            {"detail": "Backend not fully initialized"}, status_code=503
        )

    # Get skill name and file path from path
    skill_name = request.path_params.get("skill_name")
    file_path_encoded = request.path_params.get("file_path", "")
    
    if not skill_name:
        return JSONResponse(
            {"detail": "Missing skill name in path"}, status_code=400
        )
    
    if not file_path_encoded:
        return JSONResponse(
            {"detail": "Missing file path in path"}, status_code=400
        )

    # Decode URL-encoded file path
    try:
        file_path_str = unquote(file_path_encoded)
    except Exception as e:
        return JSONResponse(
            {"detail": f"Invalid file path encoding: {e}"}, status_code=400
        )

    # Find the skill directory
    skill_dir, _ = _find_skill_directory(skill_name)
    if skill_dir is None:
        return JSONResponse(
            {"detail": f"Skill '{skill_name}' not found in local storage"}, 
            status_code=404
        )

    # Build full file path
    try:
        # Prevent path traversal
        file_path = skill_dir / file_path_str
        # Ensure the file is within skill_dir
        file_path = file_path.resolve()
        skill_dir_resolved = skill_dir.resolve()
        if not str(file_path).startswith(str(skill_dir_resolved)):
            return JSONResponse(
                {"detail": "Invalid file path: path traversal detected"}, 
                status_code=400
            )
    except Exception as e:
        return JSONResponse(
            {"detail": f"Invalid file path: {e}"}, status_code=400
        )

    # Get request body
    try:
        body = await request.json()
    except Exception as e:
        return JSONResponse(
            {"detail": f"Invalid JSON in request body: {e}"}, status_code=400
        )

    content = body.get("content")
    content_base64 = body.get("content_base64")
    
    if content is None and content_base64 is None:
        return JSONResponse(
            {"detail": "Missing 'content' or 'content_base64' in request body"}, 
            status_code=400
        )

    try:
        # Create parent directories if needed
        file_path.parent.mkdir(parents=True, exist_ok=True)

        # Write file content
        if content is not None:
            # Text content
            file_path.write_text(content, encoding="utf-8")
        else:
            # Binary content from base64
            binary_content = base64.b64decode(content_base64)
            file_path.write_bytes(binary_content)

        # Reload the skill to update search index
        if config_global:
            try:
                loaded_skills = load_from_local(str(skill_dir), config_global)
                if loaded_skills:
                    await _replace_skills_with_reindex(loaded_skills)
                    logger.info(f"Reloaded skill '{skill_name}' after file update")
            except Exception as e:
                logger.warning(f"Failed to reload skill after file update: {e}")

        stat = file_path.stat()
        logger.info(f"Updated file '{file_path_str}' in skill '{skill_name}'")
        return JSONResponse({
            "skill_name": skill_name,
            "file_path": file_path_str,
            "size": stat.st_size,
            "modified": stat.st_mtime,
            "message": "File updated successfully",
        })
    except Exception as exc:
        logger.exception(f"Failed to update file '{file_path_str}' in skill '{skill_name}'")
        return JSONResponse(
            {"detail": f"Failed to update file: {exc}"}, status_code=500
        )


async def delete_skill_file(request):
    """Delete a specific file from a skill."""
    if request.method != "DELETE":  # pragma: no cover - Starlette enforces methods
        return JSONResponse({"detail": "Method not allowed"}, status_code=405)

    if config_global is None:
        return JSONResponse(
            {"detail": "Backend not fully initialized"}, status_code=503
        )

    # Get skill name and file path from path
    skill_name = request.path_params.get("skill_name")
    file_path_encoded = request.path_params.get("file_path", "")
    
    if not skill_name:
        return JSONResponse(
            {"detail": "Missing skill name in path"}, status_code=400
        )
    
    if not file_path_encoded:
        return JSONResponse(
            {"detail": "Missing file path in path"}, status_code=400
        )

    # Decode URL-encoded file path
    try:
        file_path_str = unquote(file_path_encoded)
    except Exception as e:
        return JSONResponse(
            {"detail": f"Invalid file path encoding: {e}"}, status_code=400
        )

    # Prevent deletion of SKILL.md
    if file_path_str == "SKILL.md" or file_path_str.endswith("/SKILL.md"):
        return JSONResponse(
            {"detail": "Cannot delete SKILL.md file"}, 
            status_code=400
        )

    # Find the skill directory
    skill_dir, _ = _find_skill_directory(skill_name)
    if skill_dir is None:
        return JSONResponse(
            {"detail": f"Skill '{skill_name}' not found in local storage"}, 
            status_code=404
        )

    # Build full file path
    try:
        # Prevent path traversal
        file_path = skill_dir / file_path_str
        # Ensure the file is within skill_dir
        file_path = file_path.resolve()
        skill_dir_resolved = skill_dir.resolve()
        if not str(file_path).startswith(str(skill_dir_resolved)):
            return JSONResponse(
                {"detail": "Invalid file path: path traversal detected"}, 
                status_code=400
            )
    except Exception as e:
        return JSONResponse(
            {"detail": f"Invalid file path: {e}"}, status_code=400
        )

    if not file_path.exists():
        return JSONResponse(
            {"detail": f"File '{file_path_str}' not found in skill '{skill_name}'"}, 
            status_code=404
        )

    if not file_path.is_file():
        return JSONResponse(
            {"detail": f"Path '{file_path_str}' is not a file"}, 
            status_code=400
        )

    try:
        # Delete the file
        file_path.unlink()
        
        # Reload the skill to update search index
        if config_global:
            try:
                loaded_skills = load_from_local(str(skill_dir), config_global)
                if loaded_skills:
                    await _replace_skills_with_reindex(loaded_skills)
                    logger.info(f"Reloaded skill '{skill_name}' after file deletion")
            except Exception as e:
                logger.warning(f"Failed to reload skill after file deletion: {e}")

        logger.info(f"Deleted file '{file_path_str}' from skill '{skill_name}'")
        return JSONResponse({
            "skill_name": skill_name,
            "file_path": file_path_str,
            "message": "File deleted successfully",
        })
    except Exception as exc:
        logger.exception(f"Failed to delete file '{file_path_str}' from skill '{skill_name}'")
        return JSONResponse(
            {"detail": f"Failed to delete file: {exc}"}, status_code=500
        )


async def _remove_skill_from_index(
    skill_name: str, 
    tenant_id: str | None = None, 
    agent_id: str | None = None
) -> int:
    """Remove a skill from search engine index and return remaining skill count.
    
    Parameters:
        skill_name: Name of the skill to remove
        tenant_id: Optional tenant ID to match
        agent_id: Optional agent ID to match
    """
    if search_engine is None:
        raise RuntimeError("Search engine not initialized")

    global reload_lock
    if reload_lock is None:
        reload_lock = asyncio.Lock()

    async with reload_lock:
        if loading_state_global:
            with loading_state_global._lock:
                loading_state_global.is_complete = False
                loading_state_global.loaded_skills = 0
                loading_state_global.total_skills = 0

        def _rebuild_index_without_skill() -> int:
            with search_engine._lock:  # type: ignore[attr-defined]
                existing_skills = list(search_engine.skills)

                # Filter out the skill to be deleted (match by name, tenant_id, and agent_id)
                remaining_skills = [
                    s for s in existing_skills 
                    if not (
                        s.name == skill_name 
                        and s.tenant_id == tenant_id 
                        and s.agent_id == agent_id
                    )
                ]

                if not remaining_skills:
                    logger.warning("No skills remaining after deletion")
                    search_engine.skills = []
                    search_engine.embeddings = None
                    return 0

                logger.info(f"Reindexing {len(remaining_skills)} remaining skills after deletion...")
                search_engine.skills = remaining_skills

                # Generate embeddings from skill descriptions
                descriptions = [skill.description for skill in remaining_skills]
                model = search_engine._ensure_model_loaded()
                search_engine.embeddings = model.encode(descriptions, convert_to_numpy=True)

                logger.info(f"Successfully reindexed {len(remaining_skills)} skills")
                return len(remaining_skills)

        try:
            total_skills = await asyncio.to_thread(_rebuild_index_without_skill)
        except Exception as exc:  # pragma: no cover - defensive
            if loading_state_global:
                loading_state_global.add_error(str(exc))
                with loading_state_global._lock:
                    loading_state_global.is_complete = True
            raise exc

        if loading_state_global:
            with loading_state_global._lock:
                loading_state_global.loaded_skills = total_skills
                loading_state_global.total_skills = total_skills
                loading_state_global.is_complete = True

        return total_skills


async def delete_skill(request):
    """Delete an entire skill (directory and all files)."""
    if request.method != "DELETE":  # pragma: no cover - Starlette enforces methods
        return JSONResponse({"detail": "Method not allowed"}, status_code=405)

    if config_global is None:
        return JSONResponse(
            {"detail": "Backend not fully initialized"}, status_code=503
        )

    # Get skill name from path
    skill_name = request.path_params.get("skill_name")
    
    if not skill_name:
        return JSONResponse(
            {"detail": "Missing skill name in path"}, status_code=400
        )

    # Get optional agent_id and tenant_id from query parameters
    agent_id = request.query_params.get("agent_id")
    tenant_id = request.query_params.get("tenant_id")

    # Find the skill directory
    skill_dir, skill_found = _find_skill_directory(skill_name, tenant_id, agent_id)
    if skill_dir is None:
        return JSONResponse(
            {"detail": f"Skill '{skill_name}' not found in local storage"}, 
            status_code=404
        )

    # Verify the skill directory is within the local root (security check)
    local_root = _get_primary_local_skill_root()
    if local_root is None:
        return JSONResponse(
            {"detail": "No local skill source configured"}, status_code=500
        )

    try:
        # Ensure the skill directory is within local_root
        skill_dir_resolved = skill_dir.resolve()
        local_root_resolved = local_root.resolve()
        if not str(skill_dir_resolved).startswith(str(local_root_resolved)):
            return JSONResponse(
                {"detail": "Invalid skill path: path traversal detected"}, 
                status_code=400
            )
    except Exception as e:
        return JSONResponse(
            {"detail": f"Invalid skill path: {e}"}, status_code=400
        )

    if not skill_dir.exists():
        return JSONResponse(
            {"detail": f"Skill directory for '{skill_name}' does not exist"}, 
            status_code=404
        )

    try:
        # Remove skill from search engine index first
        try:
            await _remove_skill_from_index(skill_name, tenant_id, agent_id)
            logger.info(f"Removed skill '{skill_name}' from search index (tenant_id={tenant_id}, agent_id={agent_id})")
        except Exception as e:
            logger.warning(f"Failed to remove skill '{skill_name}' from index: {e}")
            # Continue with directory deletion even if index removal fails

        # Delete the entire skill directory
        shutil.rmtree(skill_dir)
        logger.info(f"Deleted skill directory '{skill_dir}' for skill '{skill_name}'")

        return JSONResponse({
            "skill_name": skill_name,
            "message": "Skill deleted successfully",
        })
    except Exception as exc:
        logger.exception(f"Failed to delete skill '{skill_name}'")
        return JSONResponse(
            {"detail": f"Failed to delete skill: {exc}"}, status_code=500
        )


async def health_check(request):
    """Health check endpoint."""
    skills_loaded = len(search_engine.skills) if search_engine else 0
    models_loaded = search_engine.model is not None if search_engine else False

    response = {
        "status": "ok",
        "version": "1.0.6",
        "skills_loaded": skills_loaded,
        "models_loaded": models_loaded,
        "loading_complete": loading_state_global.is_complete
        if loading_state_global
        else False,
    }

    # Add auto-update information
    if config_global:
        response["auto_update_enabled"] = config_global.get(
            "auto_update_enabled", False
        )

    if scheduler_global:
        scheduler_status = scheduler_global.get_status()
        response.update(
            {
                "next_update_check": scheduler_status.get("next_run_time"),
                "last_update_check": scheduler_status.get("last_run_time"),
            }
        )

    if update_checker_global:
        api_usage = update_checker_global.get_api_usage()
        response.update(
            {
                "github_api_calls_this_hour": api_usage.get("calls_this_hour", 0),
                "github_api_limit": api_usage.get("limit_per_hour", 60),
                "github_authenticated": api_usage.get("authenticated", False),
            }
        )

    if loading_state_global:
        with loading_state_global._lock:
            if loading_state_global.errors:
                response["update_errors"] = loading_state_global.errors[
                    -5:
                ]  # Last 5 errors

    return JSONResponse(response)


async def initialize_backend(config_path: str | None = None, verbose: bool = False):
    """Initialize search engine and load skills."""
    global \
        search_engine, \
        loading_state_global, \
        update_checker_global, \
        scheduler_global, \
        config_global

    # Setup logging
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    logger.info("Initializing Claude Skills MCP Backend")

    # Load configuration
    config = load_config(config_path)
    config_global = config

    # Initialize search engine
    logger.info("Initializing search engine...")
    search_engine = SkillSearchEngine(config["embedding_model"])

    # Initialize loading state
    loading_state_global = LoadingState()

    # Initialize update checker
    github_token = config.get("github_api_token")
    update_checker_global = UpdateChecker(github_token)
    logger.info(
        f"Update checker initialized (GitHub token: {'provided' if github_token else 'not provided'})"
    )

    # Register MCP tools
    register_mcp_tools(
        default_top_k=config["default_top_k"],
        max_content_chars=config.get("max_skill_content_chars"),
    )

    # Define batch callback for incremental loading
    def on_batch_loaded(batch_skills: list, total_loaded: int) -> None:
        logger.info(f"Batch loaded: {len(batch_skills)} skills (total: {total_loaded})")
        search_engine.add_skills(batch_skills)
        loading_state_global.update_progress(total_loaded)

    # Start background thread to load skills
    def background_loader() -> None:
        try:
            logger.info("Starting background skill loading...")
            load_skills_in_batches(
                skill_sources=config["skill_sources"],
                config=config,
                batch_callback=on_batch_loaded,
                batch_size=config.get("batch_size", 10),
            )
            loading_state_global.mark_complete()
            logger.info("Background skill loading complete")
        except Exception as e:
            logger.error(f"Error in background loading: {e}", exc_info=True)
            loading_state_global.add_error(str(e))
            loading_state_global.mark_complete()

    # Start the background loading thread
    loader_thread = threading.Thread(target=background_loader, daemon=True)
    loader_thread.start()
    logger.info("Background loading thread started, server is ready")

    # Setup auto-update scheduler if enabled
    if config.get("auto_update_enabled", False):
        interval_minutes = config.get("auto_update_interval_minutes", 60)

        async def update_callback():
            """Callback for scheduled updates."""
            try:
                logger.info("Running scheduled update check...")

                # Check for updates
                result = update_checker_global.check_for_updates(
                    config["skill_sources"]
                )

                logger.info(
                    f"Update check complete: {len(result.changed_sources)} sources changed, "
                    f"{result.api_calls_made} API calls made"
                )

                if result.errors:
                    for error in result.errors:
                        logger.warning(f"Update check error: {error}")
                        loading_state_global.add_error(error)

                # Reload skills if updates detected
                if result.has_updates:
                    logger.info(
                        f"Reloading {len(result.changed_sources)} changed sources..."
                    )

                    # For simplicity, reload all skills if any changed
                    # This clears the index and reloads everything
                    logger.info("Reloading all skills...")
                    new_skills = load_all_skills(
                        skill_sources=config["skill_sources"], config=config
                    )

                    # Re-index all skills
                    search_engine.index_skills(new_skills)
                    logger.info(f"Re-indexed {len(new_skills)} skills after update")
                else:
                    logger.info("No updates detected")

                # Warn if approaching API limit (only for non-authenticated)
                api_usage = update_checker_global.get_api_usage()
                if (
                    not api_usage["authenticated"]
                    and api_usage["calls_this_hour"] >= 50
                ):
                    logger.warning(
                        f"Approaching GitHub API rate limit: {api_usage['calls_this_hour']}/60 calls this hour"
                    )

            except Exception as e:
                error_msg = f"Error during scheduled update: {e}"
                logger.error(error_msg, exc_info=True)
                loading_state_global.add_error(error_msg)

        # Create and start scheduler
        scheduler_global = HourlyScheduler(interval_minutes, update_callback)
        scheduler_global.start()
        logger.info(
            f"Auto-update scheduler started (interval: {interval_minutes} minutes)"
        )
    else:
        logger.info("Auto-update disabled in configuration")


async def run_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    config_path: str | None = None,
    verbose: bool = False,
):
    """Run the HTTP server using FastMCP with custom routes."""
    # Initialize backend (search engine, skills, etc.)
    await initialize_backend(config_path, verbose)

    # Get FastMCP's ASGI app (includes /mcp route internally)
    app = get_application()

    # Run server with uvicorn
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="debug" if verbose else "info"
    )
    server = uvicorn.Server(config)
    await server.serve()


def _ensure_routes(app):
    """Ensure custom routes are attached to the FastMCP app exactly once."""
    existing_paths = {getattr(route, "path", None) for route in getattr(app, "routes", [])}
    if "/skills/upload" not in existing_paths:
        app.routes.insert(
            0, Route("/skills/upload", upload_skill_archive, methods=["POST"])
        )
    if "/skills/upload-from-github" not in existing_paths:
        app.routes.insert(
            0, Route("/skills/upload-from-github", upload_skill_from_github, methods=["POST"])
        )
    if "/skills/download" not in existing_paths:
        app.routes.insert(
            0, Route("/skills/download", download_skill_archive, methods=["GET"])
        )
    if "/skills/list" not in existing_paths:
        app.routes.insert(
            0, Route("/skills/list", list_uploaded_skills, methods=["GET"])
        )
    if "/skills/list-builtin" not in existing_paths:
        app.routes.insert(
            0, Route("/skills/list-builtin", list_builtin_skills, methods=["GET"])
        )
    # Skill deletion route (must be added before file routes to match correctly)
    skill_delete_path = "/skills/{skill_name}"
    if skill_delete_path not in existing_paths:
        app.routes.insert(
            0, Route(skill_delete_path, delete_skill, methods=["DELETE"])
        )
    # File management routes (must be added before other routes to match correctly)
    file_list_path = "/skills/{skill_name}/files"
    file_detail_path = "/skills/{skill_name}/files/{file_path:path}"
    
    if file_list_path not in existing_paths:
        app.routes.insert(
            0, Route(file_list_path, list_skill_files, methods=["GET"])
        )
    if file_detail_path not in existing_paths:
        app.routes.insert(
            0, Route(file_detail_path, get_skill_file, methods=["GET"])
        )
        app.routes.insert(
            0, Route(file_detail_path, update_skill_file, methods=["PUT"])
        )
        app.routes.insert(
            0, Route(file_detail_path, delete_skill_file, methods=["DELETE"])
        )
    if "/skills/check" not in existing_paths:
        app.routes.insert(
            0, Route("/skills/check", check_skill, methods=["GET"])
        )
    if "/health" not in existing_paths:
        app.routes.insert(0, Route("/health", health_check, methods=["GET"]))


def get_application():
    """Return FastMCP ASGI app with custom routes registered."""
    global _routes_initialized
    app = mcp.streamable_http_app()
    _ensure_routes(app)
    _routes_initialized = True
    return app
