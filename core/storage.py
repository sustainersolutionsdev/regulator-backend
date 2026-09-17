from google.cloud import storage
from datetime import datetime, timezone
import uuid

BUCKET_NAME = "regulator-dev-documents"

# Confirmed by Srinivas (9/17): PDF/Word/Excel/text plus PowerPoint, 25MB.
ALLOWED_DOCUMENT_EXTENSIONS = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".txt", ".pptx", ".ppt"}
MAX_DOCUMENT_BYTES = 25 * 1024 * 1024

_storage_client = storage.Client(project="regulator-dev")


def upload_document(
    tenant_id: str,
    file_bytes: bytes,
    filename: str,
    content_type: str,
    actor_uid: str,
) -> dict:
    """
    Uploads one supporting document ahead of regulation creation, to
    tenants/{tenant_id}/regulation-documents/{uuid}-{filename} — tenant-
    scoped in the object path itself, same isolation principle as every
    Firestore path in this codebase (NFR-1).

    Returns metadata only (never the raw bytes) — this is what gets
    embedded in the regulation record's `documents` array, not the file
    content itself.
    """
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ALLOWED_DOCUMENT_EXTENSIONS:
        raise ValueError(
            f"File type '{ext}' is not allowed. Allowed types: "
            f"{', '.join(sorted(ALLOWED_DOCUMENT_EXTENSIONS))}"
        )

    if len(file_bytes) > MAX_DOCUMENT_BYTES:
        raise ValueError(f"File exceeds the {MAX_DOCUMENT_BYTES // (1024 * 1024)}MB limit.")

    blob_path = f"tenants/{tenant_id}/regulation-documents/{uuid.uuid4()}-{filename}"
    bucket = _storage_client.bucket(BUCKET_NAME)
    blob = bucket.blob(blob_path)
    blob.upload_from_string(file_bytes, content_type=content_type)

    return {
        "fileName": filename,
        "gcsUri": f"gs://{BUCKET_NAME}/{blob_path}",
        "contentType": content_type,
        "sizeBytes": len(file_bytes),
        "uploadedBy": actor_uid,
        "uploadedAt": datetime.now(timezone.utc),
    }
