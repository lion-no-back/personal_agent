import hashlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from .types import ParsedElement

if TYPE_CHECKING:
    from unstructured_paddleocr import PaddleOCR as _PaddleOCR

logger = logging.getLogger(__name__)

_ocr_instance: "_PaddleOCR | None" = None


def _get_ocr() -> "_PaddleOCR":
    global _ocr_instance
    if _ocr_instance is None:
        from unstructured_paddleocr import PaddleOCR
        _ocr_instance = PaddleOCR(use_angle_cls=True, lang="ch", use_gpu=False, show_log=False)
    return _ocr_instance


def parse_image(path: Path, llm_client=None) -> list[ParsedElement]:
    # All elements from one image file share a group_id so the chunker
    # merges them into a parent chunk instead of creating isolated shards.
    gid = hashlib.sha256(str(path).encode()).hexdigest()[:16]

    elements: list[ParsedElement] = []

    # Strategy 1: semantic description via markitdown (LLM-driven when client provided)
    try:
        from markitdown import MarkItDown
        kwargs: dict = {}
        if llm_client is not None:
            kwargs["llm_client"] = llm_client
            kwargs["llm_model"] = "claude-haiku-4-5-20251001"
        md = MarkItDown(**kwargs).convert(str(path)).text_content.strip()
        if md:
            elements.append(ParsedElement(
                type="image_description",
                content=md,
                page_num=None,
                level=None,
                bbox=None,
                group_id=gid,
            ))
    except Exception as e:
        logger.warning("markitdown image description failed (%s): %s", path.name, e)

    # Strategy 2: OCR via PaddleOCR called directly (not through unstructured's partition_image).
    # Calling directly lets us pass enable_mkldnn=False — the unstructured wrapper has no
    # parameter for this, and the env-var approach (FLAGS_use_mkldnn=0) races with paddle's
    # C++ static init on Windows, causing fused_conv2d crashes.
    try:
        # ensure_paddle MUST come first — cv2 is not yet loaded at this point,
        # so the installer can write cv2.pyd without hitting Windows file locks.
        from ...paddle_bootstrap import ensure_paddle
        ensure_paddle()

        try:
            _get_ocr()  # validate importable before use; raises RuntimeError with clear message
        except (ImportError, OSError) as exc:
            raise RuntimeError(
                "unstructured_paddleocr not importable "
                f"({type(exc).__name__}); try restarting the process"
            ) from exc

        import numpy as np
        from PIL import Image as PILImage

        logger.info("PaddleOCR starting on %s", path.name)
        img_array = np.array(PILImage.open(str(path)).convert("RGB"))
        result = _get_ocr().ocr(img_array, cls=True)

        ocr_count = 0
        for page_result in (result or []):
            for line in (page_result or []):
                _bbox, (text, _conf) = line
                text = text.strip()
                if text:
                    ocr_count += 1
                    elements.append(ParsedElement(
                        type="image_ocr",
                        content=text,
                        page_num=None,
                        level=None,
                        bbox=None,
                        group_id=gid,
                    ))

        if ocr_count == 0:
            logger.warning(
                "PaddleOCR found no text in %s — blank image or unrecognized layout",
                path.name,
            )
        else:
            logger.info("PaddleOCR extracted %d block(s) from %s", ocr_count, path.name)

    except Exception as e:
        logger.error("PaddleOCR pipeline failed for %s: %s", path.name, e, exc_info=True)
        # Re-raise so pipeline.ingest() knows extraction failed and skips writing
        # the source record — this lets the next ingest attempt retry instead of
        # treating a failed run as "file already processed".
        raise

    return elements
