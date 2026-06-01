import logging
from pathlib import Path

from .types import ParsedElement

logger = logging.getLogger(__name__)


def parse_image(path: Path, llm_client=None) -> list[ParsedElement]:
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
            ))
    except Exception as e:
        logger.warning("markitdown image description failed (%s): %s", path.name, e)

    # Strategy 2: OCR via PaddleOCR + unstructured
    try:
        # ensure_paddle MUST come first — cv2 is not yet loaded at this point,
        # so the installer can write cv2.pyd without hitting Windows file locks.
        from ...paddle_bootstrap import ensure_paddle
        ensure_paddle()

        from ...hf_bootstrap import ensure_hf_model
        ensure_hf_model("unstructuredio/detectron2_faster_rcnn_R_50_FPN_3x")

        # Validate the agent module is importable before passing its name as a string.
        # paddle_bootstrap may succeed (pip exits 0) yet the module remain invisible
        # to the current process if importlib caches weren't refreshed — this surfaces
        # that failure as a clear RuntimeError rather than a cryptic unstructured error.
        try:
            import unstructured_paddleocr as _  # noqa: F401
        except (ImportError, OSError) as exc:
            raise RuntimeError(
                "unstructured_paddleocr not importable "
                f"({type(exc).__name__}); try restarting the process"
            ) from exc

        from unstructured.partition.image import partition_image
        from unstructured.partition.utils.constants import OCR_AGENT_PADDLE
        logger.info("PaddleOCR starting on %s", path.name)
        raw = partition_image(
            filename=str(path),
            strategy="hi_res",
            ocr_agent=OCR_AGENT_PADDLE,
            languages=["ch"],
        )

        ocr_count = 0
        for elem in raw:
            text = str(elem).strip()
            if text:
                ocr_count += 1
                elements.append(ParsedElement(
                    type="image_ocr",
                    content=text,
                    page_num=None,
                    level=None,
                    bbox=None,
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
