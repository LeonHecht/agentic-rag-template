from fastapi import APIRouter, UploadFile, File, HTTPException, Form, Depends
from pathlib import Path
from typing import List
import uuid
import re

import pandas as pd

from backend.app.services.search import search_engine
from backend.app.services.analytics import load_csv_to_duckdb
from backend.app.core.config import settings
from backend.app.dependencies import get_current_user
from backend.app.services.auth import get_accessible_spaces, UserData

router = APIRouter()

# Base folder for uploads
UPLOADS_ROOT = Path(settings.DATA_UPLOAD)
UPLOADS_ROOT.mkdir(parents=True, exist_ok=True)


EXCEL_EXTENSIONS = {".xlsx", ".xlsm"}


def _safe_sheet_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", (name or "sheet").strip())
    cleaned = cleaned.strip("._-")
    return cleaned[:80] or "sheet"


def _convert_workbook_to_csvs(workbook_path: Path, output_dir: Path) -> list[dict]:
    """Convert every worksheet in one Excel workbook into a generated CSV."""
    try:
        sheets = pd.read_excel(workbook_path, sheet_name=None, engine="openpyxl")
    except ImportError as exc:
        raise RuntimeError("Reading Excel workbooks requires openpyxl. Install backend requirements.") from exc
    except Exception as exc:
        raise RuntimeError(f"Failed to read Excel workbook: {exc}") from exc

    converted = []
    used_names: set[str] = set()
    for sheet_name, frame in sheets.items():
        safe_name = _safe_sheet_name(str(sheet_name))
        candidate = f"{workbook_path.stem}__{safe_name}.csv"
        suffix = 2
        while candidate.lower() in used_names:
            candidate = f"{workbook_path.stem}__{safe_name}_{suffix}.csv"
            suffix += 1
        used_names.add(candidate.lower())

        csv_path = output_dir / candidate
        frame.to_csv(csv_path, index=False)
        converted.append({
            "sheet": str(sheet_name),
            "filename": candidate,
            "saved_path": csv_path.name,
        })
    return converted


@router.post("/upload", summary="Upload one or multiple documents into a space")
async def upload_file(
    files: List[UploadFile] = File(...),
    space: str = Form("default"),
    user: UserData = Depends(get_current_user),
):
    """
    files: list of UploadFile
    space: the name of the space (folder) under UPLOADS_ROOT
    """
    if space not in get_accessible_spaces(user):
        raise HTTPException(403, detail="Space not accessible")

    excel_files = [
        file for file in files
        if Path(file.filename or "").suffix.lower() in EXCEL_EXTENSIONS
    ]
    if len(excel_files) > 1:
        raise HTTPException(400, detail="Only one Excel workbook can be uploaded at a time.")
    if excel_files and len(files) > 1:
        raise HTTPException(400, detail="Upload either CSV files or one Excel workbook, not both.")

    space_dir = UPLOADS_ROOT / space
    space_dir.mkdir(parents=True, exist_ok=True)

    saved = []
    converted = []
    analytics_loaded = []
    for file in files:
        # Unique file ID + preserve extension
        file_id = uuid.uuid4().hex
        original_filename = file.filename or "upload"
        ext = Path(original_filename).suffix
        dest = space_dir / f"{file_id}{ext}"
        try:
            contents = await file.read()
            dest.write_bytes(contents)
        except Exception as e:
            raise HTTPException(500, f"Error saving {file.filename}: {e}")

        saved.append({
            "file_id": file_id,
            "filename": original_filename,
            "saved_path": dest.name,
        })

        if ext.lower() == ".csv":
            try:
                result = load_csv_to_duckdb(dest)
                result.update({"filename": original_filename, "saved_path": dest.name})
                analytics_loaded.append(result)
            except Exception as e:
                analytics_loaded.append({
                    "loaded": False,
                    "filename": original_filename,
                    "saved_path": dest.name,
                    "reason": f"Failed to load CSV into DuckDB: {e}",
                })
        elif ext.lower() in EXCEL_EXTENSIONS:
            try:
                generated_csvs = _convert_workbook_to_csvs(dest, space_dir)
            except Exception as e:
                raise HTTPException(400, detail=f"Failed to convert Excel workbook: {e}")

            converted.extend(generated_csvs)
            for csv_info in generated_csvs:
                csv_path = space_dir / csv_info["saved_path"]
                try:
                    result = load_csv_to_duckdb(csv_path)
                    result.update({
                        "filename": csv_info["filename"],
                        "saved_path": csv_info["saved_path"],
                        "source_workbook": original_filename,
                        "sheet": csv_info["sheet"],
                    })
                    analytics_loaded.append(result)
                except Exception as e:
                    analytics_loaded.append({
                        "loaded": False,
                        "filename": csv_info["filename"],
                        "saved_path": csv_info["saved_path"],
                        "source_workbook": original_filename,
                        "sheet": csv_info["sheet"],
                        "reason": f"Failed to load converted CSV into DuckDB: {e}",
                    })

    # Rebuild index for this space so the new docs are searchable
    search_engine.index(space)

    return {
        "space": space,
        "uploaded": saved,
        "converted": converted,
        "analytics": analytics_loaded,
    }
