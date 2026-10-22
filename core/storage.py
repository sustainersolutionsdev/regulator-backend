from google.cloud import storage
from datetime import datetime, timezone
import re
import uuid

from core.config import DOCUMENTS_BUCKET, GCP_PROJECT

BUCKET_NAME = DOCUMENTS_BUCKET  # OPEN-11: "<project>-documents" unless DOCUMENTS_BUCKET is set

# Confirmed by Srinivas (9/17): PDF/Word/Excel/text plus PowerPoint, 25MB.
# T022: each allowed extension maps to the one content type the file is
# stored and served with. The browser-supplied type is never trusted, so a
# file can't claim to be a web page (e.g. "notes.txt" sent as text/html).
CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".txt": "text/plain",
}
ALLOWED_DOCUMENT_EXTENSIONS = set(CONTENT_TYPES)
MAX_DOCUMENT_BYTES = 25 * 1024 * 1024
MAX_FILENAME_LENGTH = 200

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

_storage_client = storage.Client(project=GCP_PROJECT)


def clean_filename(filename: str) -> str:
    """
    T022: keeps only the file's own name. Browsers send just the name, but
    a hand-made request can send a path ("../../x.pdf") or control
    characters; those never reach the storage path or the screen.
    """
    name = (filename or "").replace("\\", "/").split("/")[-1]
    name = _CONTROL_CHARS.sub("", name).strip().strip(".")
    if len(name) > MAX_FILENAME_LENGTH:
        stem, dot, ext = name.rpartition(".")
        name = (stem[: MAX_FILENAME_LENGTH - len(ext) - 1] + dot + ext) if dot else name[:MAX_FILENAME_LENGTH]
    return name


def file_extension(filename: str) -> str:
    return "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def content_type_for(filename: str) -> str:
    """The stored/served type for a file name; unknown types download as bytes."""
    return CONTENT_TYPES.get(file_extension(filename or ""), "application/octet-stream")


def upload_document(
    tenant_id: str,
    file_bytes: bytes,
    filename: str,
    content_type: str,
    actor_uid: str,
) -> dict:
    """
    Uploads one supporting document to
    tenants/{tenant_id}/regulation-documents/{uuid}-{filename} - tenant-
    scoped in the object path itself, same isolation principle as every
    Firestore path in this codebase (NFR-1).

    `content_type` (what the browser claimed) is accepted for compatibility
    but no longer used (T022): the type comes from the extension.

    Returns metadata only (never the raw bytes) - this is what gets
    embedded in the regulation record's `documents` array, not the file
    content itself.
    """
    filename = clean_filename(filename)
    ext = file_extension(filename)
    if not filename or ext not in ALLOWED_DOCUMENT_EXTENSIONS:
        raise ValueError(
            f"File type '{ext}' is not allowed. Allowed types: "
            f"{', '.join(sorted(ALLOWED_DOCUMENT_EXTENSIONS))}"
        )

    if len(file_bytes) > MAX_DOCUMENT_BYTES:
        raise ValueError(f"File exceeds the {MAX_DOCUMENT_BYTES // (1024 * 1024)}MB limit.")

    stored_type = CONTENT_TYPES[ext]
    blob_path = f"tenants/{tenant_id}/regulation-documents/{uuid.uuid4()}-{filename}"
    bucket = _storage_client.bucket(BUCKET_NAME)
    blob = bucket.blob(blob_path)
    blob.upload_from_string(file_bytes, content_type=stored_type)

    return {
        "fileName": filename,
        "gcsUri": f"gs://{BUCKET_NAME}/{blob_path}",
        "contentType": stored_type,
        "sizeBytes": len(file_bytes),
        "uploadedBy": actor_uid,
        "uploadedAt": datetime.now(timezone.utc),
    }
