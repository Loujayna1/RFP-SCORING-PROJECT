#!/usr/bin/env python3
"""Pre-download Docling models for offline use (Docker build / air-gapped deploy).

docling==2.15.1 has no `docling-tools` CLI and no `model_downloader` module.
We snapshot the Hugging Face repo into DOCLING_ARTIFACTS_PATH, then verify with
a one-page PDF conversion.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_MINIMAL_PDF = (
    b"%PDF-1.0\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/MediaBox[0 0 612 792]/Contents 4 0 R"
    b"/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
    b"4 0 obj<</Length 44>>stream\n"
    b"BT /F1 24 Tf 72 720 Td (Docling warmup) Tj ET\n"
    b"endstream endobj\n"
    b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
    b"xref\n0 6\n0000000000 65535 f \n"
    b"0000000009 00000 n \n0000000058 00000 n \n0000000115 00000 n \n"
    b"0000000229 00000 n \n0000000327 00000 n \n"
    b"trailer<</Size 6/Root 1 0 R>>\nstartxref\n403\n%%EOF\n"
)


def _download_models(output_dir: Path) -> None:
    from huggingface_hub import snapshot_download

    print(f"Downloading ds4sd/docling-models -> {output_dir} ...")
    snapshot_download(
        repo_id="ds4sd/docling-models",
        local_dir=str(output_dir),
    )


def _verify_layout_model(output_dir: Path) -> None:
    layout_weights = output_dir / "model_artifacts" / "layout" / "model.safetensors"
    if not layout_weights.is_file():
        raise FileNotFoundError(f"Missing layout weights: {layout_weights}")


def _warmup_via_converter(output_dir: Path) -> None:
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    os.environ["DOCLING_ARTIFACTS_PATH"] = str(output_dir)
    pipeline_options = PdfPipelineOptions(artifacts_path=str(output_dir))
    converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options),
        }
    )

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(_MINIMAL_PDF)
        tmp_path = tmp.name

    try:
        print("Running Docling warmup conversion ...")
        converter.convert(tmp_path)
    finally:
        os.unlink(tmp_path)


def main() -> int:
    output_dir = Path(os.environ.get("DOCLING_ARTIFACTS_PATH", "/opt/docling/models"))
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        try:
            from docling.utils.model_downloader import download_models

            print(f"Downloading via docling.utils.model_downloader -> {output_dir}")
            download_models(output_dir=output_dir, progress=True)
        except ImportError:
            _download_models(output_dir)
            _verify_layout_model(output_dir)
            _warmup_via_converter(output_dir)
    except Exception as exc:
        print(f"ERROR: Docling model prefetch failed: {exc}", file=sys.stderr)
        return 1

    print(f"Docling models ready at {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
