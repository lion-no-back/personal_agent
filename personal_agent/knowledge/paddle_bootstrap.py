"""
Auto-installs paddlepaddle + unstructured-paddleocr on first use.
Fast no-op if both are already importable. Mirrors java_bootstrap.py pattern.

Must be called BEFORE any unstructured/cv2 imports so that cv2.pyd is not
yet locked by the process — allowing the installer to place files freely.
"""
import importlib
import importlib.util
import logging
import subprocess
import sys
import threading

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_ensured = False

_PACKAGES = ["paddlepaddle", "unstructured-paddleocr"]


def ensure_paddle() -> None:
    """Install paddle packages if absent, then ensure torch is loadable.
    Thread-safe; subsequent calls are instant."""
    global _ensured
    if _ensured:
        return
    with _lock:
        if _ensured:
            return
        import os
        import warnings
        # Disable OneDNN (MKL-DNN) before any paddle import — Windows fused_conv2d
        # crashes with "OneDnnContext does not have the input Filter" when OneDNN is on.
        os.environ.setdefault("FLAGS_use_mkldnn", "0")
        # Paddle checks for ccache (C++ compiler cache) on every import. We only use
        # pre-built paddle binaries and never compile C++ extensions, so suppress it.
        warnings.filterwarnings("ignore", message="No ccache found", category=UserWarning)
        _ensure_opencv_contrib()   # swap headless→contrib before cv2 is loaded
        if not _is_available():
            _install()
        _fix_torch_if_broken()
        _patch_inference_ir_opt()
        _ensured = True


def _pkg_installed(name: str) -> bool:
    try:
        from importlib.metadata import version
        version(name)
        return True
    except Exception:
        return False


def _ensure_opencv_contrib() -> None:
    """Swap opencv-python-headless → opencv-contrib-python if needed.

    unstructured-paddleocr requires opencv-contrib-python specifically.
    Both packages install the same cv2.pyd file, so only one can exist.
    We swap before anything imports cv2; once cv2 is loaded the DLL is
    locked on Windows and can't be replaced until the process restarts.
    """
    if _pkg_installed("opencv-contrib-python"):
        return  # already correct, nothing to do

    if not _pkg_installed("opencv-python-headless"):
        return  # neither installed; _install() will pull in contrib via unstructured-paddleocr

    if "cv2" in sys.modules:
        # DLL already loaded — can't swap in this process run.
        # _install() will use --no-deps to avoid touching cv2.pyd.
        logger.warning(
            "[paddle_bootstrap] opencv-python-headless is loaded and DLL-locked; "
            "cannot auto-swap to opencv-contrib-python. "
            "Restart the process once to let the swap happen at startup."
        )
        return

    import shutil
    uv = shutil.which("uv")
    env = {**__import__("os").environ, "UV_LINK_MODE": "copy"}
    logger.info("[paddle_bootstrap] auto-swapping opencv-python-headless → opencv-contrib-python")
    try:
        from personal_agent.workflows.deplock import install_lock
        with install_lock():  # 跨进程串行装包，避免与工作流子进程并发改写共享 venv
            if uv:
                subprocess.check_call([uv, "pip", "uninstall", "opencv-python-headless"], env=env)
                subprocess.check_call([uv, "pip", "install", "opencv-contrib-python"], env=env)
            else:
                subprocess.check_call(
                    [sys.executable, "-m", "pip", "uninstall", "opencv-python-headless", "-y"], env=env
                )
                subprocess.check_call(
                    [sys.executable, "-m", "pip", "install", "opencv-contrib-python"], env=env
                )
        importlib.invalidate_caches()
        logger.info("[paddle_bootstrap] opencv-contrib-python installed")
    except subprocess.CalledProcessError as e:
        logger.error("[paddle_bootstrap] opencv swap failed: %s", e)
        # Non-fatal: _install() will proceed with --no-deps fallback


def _is_available() -> bool:
    return (
        importlib.util.find_spec("paddle") is not None
        and importlib.util.find_spec("unstructured_paddleocr") is not None
    )


def _install() -> None:
    import shutil

    uv = shutil.which("uv")
    env = {**__import__("os").environ, "UV_LINK_MODE": "copy"}

    # If cv2 is already loaded in this process the DLL is locked on Windows —
    # installing opencv-contrib-python (a dep of unstructured-paddleocr) would try
    # to rename cv2.pyd and fail with "拒绝访问". Install unstructured-paddleocr
    # without deps in that case; cv2 is already functional regardless of which
    # opencv variant provided it.
    # Only sys.modules tells us whether cv2.pyd is actually loaded (and DLL-locked).
    # find_spec("cv2") just checks if the package exists on disk — that alone does
    # NOT lock the file. Using find_spec here caused --no-deps to fire every time
    # any opencv package was installed, silently skipping all transitive deps.
    cv2_locked = "cv2" in sys.modules

    print(f"[paddle_bootstrap] Installing {', '.join(_PACKAGES)} ...", file=sys.stderr)
    try:
        from personal_agent.workflows.deplock import install_lock
        with install_lock():  # 跨进程串行装包，避免与工作流子进程并发改写共享 venv
            if uv:
                # Always install paddlepaddle normally.
                subprocess.check_call(
                    [uv, "pip", "install", "--reinstall-package", "paddlepaddle", "paddlepaddle"],
                    env=env,
                )
                # Install unstructured-paddleocr: skip deps if cv2 is locked to avoid
                # replacing cv2.pyd; otherwise do a full install so opencv-contrib-python
                # lands correctly.
                undep_flag = ["--no-deps"] if cv2_locked else []
                subprocess.check_call(
                    [uv, "pip", "install", "--reinstall-package", "unstructured-paddleocr"]
                    + undep_flag
                    + ["unstructured-paddleocr"],
                    env=env,
                )
            else:
                subprocess.check_call(
                    [sys.executable, "-m", "pip", "install"] + _PACKAGES, env=env
                )
        importlib.invalidate_caches()
        print("[paddle_bootstrap] Installation complete.", file=sys.stderr)
        logger.info("[paddle_bootstrap] paddlepaddle + unstructured-paddleocr installed")
    except subprocess.CalledProcessError as e:
        logger.error("[paddle_bootstrap] Installation failed: %s", e)
        raise RuntimeError(
            "Failed to install PaddleOCR dependencies. "
            "Run manually: uv pip install paddlepaddle unstructured-paddleocr"
        ) from e


def _patch_inference_ir_opt() -> None:
    """Monkey-patch paddle.inference.create_predictor to disable IR optimization.

    On Windows CPU builds, paddle's IR optimizer fuses conv2d into fused_conv2d
    (an OneDNN operator) even when enable_mkldnn=False.  fused_conv2d then fails
    with "OneDnnContext does not have the input Filter" because the OneDNN context
    was never properly initialised for CPU-only inference.

    Disabling IR optimisation (switch_ir_optim(False)) prevents the fusion;
    plain conv2d is used instead, which runs correctly without OneDNN.
    """
    try:
        import paddle.inference as paddle_inf

        _orig_create = paddle_inf.create_predictor

        def _safe_create(config):
            config.switch_ir_optim(False)
            return _orig_create(config)

        paddle_inf.create_predictor = _safe_create
        logger.info("[paddle_bootstrap] patched create_predictor: IR opt disabled (Windows fused_conv2d fix)")
    except Exception as exc:
        logger.warning("[paddle_bootstrap] could not patch create_predictor: %s", exc)


def _is_torch_dll_error(exc: OSError) -> bool:
    err = str(exc)
    return "shm.dll" in err or "WinError 127" in err


def _fix_torch_if_broken() -> None:
    """Ensure torch is importable, falling back to stub if DLLs are unresolvable.

    Strategy:
      1. If torch loads fine → done.
      2. If GPU torch fails (shm.dll / WinError 127) → reinstall CPU-only build.
      3. If CPU torch ALSO fails (missing system MSVC DLLs) → stub torch and
         albumentations.pytorch so the import chain succeeds without torch.

    PaddleOCR inference uses paddlepaddle, never torch. Torch is only pulled in
    because albumentations/__init__.py does `from .pytorch import *` unconditionally.
    Stubbing it is safe for all OCR use cases.
    """
    if importlib.util.find_spec("torch") is None:
        # torch not installed at all — stub immediately so albumentations can load
        _stub_torch()
        return

    try:
        import torch  # noqa: F401
        return  # loads fine
    except OSError as exc:
        if not _is_torch_dll_error(exc):
            raise
        logger.warning("[paddle_bootstrap] torch GPU DLL failed (%s); trying CPU build", exc)

    # Evict broken torch from module cache before reinstall
    for key in [k for k in sys.modules if k == "torch" or k.startswith("torch.")]:
        del sys.modules[key]

    _install_torch_cpu()

    # Verify the reinstalled CPU build actually loads
    try:
        importlib.invalidate_caches()
        import torch  # noqa: F401
        logger.info("[paddle_bootstrap] CPU-only torch loaded successfully")
    except OSError as exc:
        if not _is_torch_dll_error(exc):
            raise
        # CPU torch also fails — system is missing MSVC runtime DLLs.
        # Stub torch so albumentations can import without it.
        logger.warning(
            "[paddle_bootstrap] CPU torch also fails (missing system DLLs: %s). "
            "Stubbing torch/albumentations.pytorch — PaddleOCR inference is unaffected.",
            exc,
        )
        for key in [k for k in sys.modules if k == "torch" or k.startswith("torch.")]:
            del sys.modules[key]
        _stub_torch()


def _stub_torch() -> None:
    """Stub torch and albumentations.pytorch so the import chain succeeds.

    albumentations/__init__.py does `from .pytorch import *` which imports torch.
    PaddleOCR inference never calls ToTensorV2 or any torch op, so stubbing is safe.
    The stub must be placed in sys.modules BEFORE albumentations is imported.
    """
    import types

    if "torch" not in sys.modules:
        sys.modules["torch"] = types.ModuleType("torch")

    # albumentations/__init__.py: from .pytorch import *
    # albumentations/pytorch/__init__.py: from .transforms import *  (imports torch)
    # Pre-populate both so the wildcard import finds an empty-but-valid module.
    alb_pt = types.ModuleType("albumentations.pytorch")
    alb_pt.ToTensorV2 = None  # stub the only symbol typically re-exported
    sys.modules.setdefault("albumentations.pytorch", alb_pt)
    sys.modules.setdefault("albumentations.pytorch.transforms", alb_pt)

    # If albumentations itself was already partially imported (and may have a
    # broken __init__ in sys.modules), evict and let it re-import with the stub in place.
    if "albumentations" in sys.modules:
        del sys.modules["albumentations"]

    logger.info("[paddle_bootstrap] torch/albumentations.pytorch stubbed for OCR-only use")


def _install_torch_cpu() -> None:
    import shutil

    uv = shutil.which("uv")
    cpu_index = "https://download.pytorch.org/whl/cpu"
    if uv:
        cmd = [uv, "pip", "install", "--force-reinstall", "torch", "--index-url", cpu_index]
    else:
        cmd = [sys.executable, "-m", "pip", "install", "--force-reinstall",
               "torch", "--index-url", cpu_index]

    try:
        from personal_agent.workflows.deplock import install_lock
        with install_lock():  # 跨进程串行装包，避免与工作流子进程并发改写共享 venv
            subprocess.check_call(cmd, env={**__import__("os").environ, "UV_LINK_MODE": "copy"})
        logger.info("[paddle_bootstrap] CPU-only torch installed")
    except subprocess.CalledProcessError as e:
        logger.error("[paddle_bootstrap] Failed to install CPU-only torch: %s", e)
        # Non-fatal: _fix_torch_if_broken will fall through to _stub_torch
