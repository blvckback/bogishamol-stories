"""Конвертация в PDF, подсчёт страниц и отправка на принтер."""
import asyncio
import shutil
import sys
from pathlib import Path

from pypdf import PdfReader
from PIL import Image

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
OFFICE_EXT = {".doc", ".docx", ".odt", ".rtf", ".ppt", ".pptx", ".xls", ".xlsx", ".txt"}
ALLOWED_EXT = IMAGE_EXT | OFFICE_EXT | {".pdf"}


async def _run(*cmd: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {err.decode(errors='ignore')}")


async def to_pdf(path: Path) -> Path:
    """Возвращает путь к PDF (конвертирует, если файл не PDF)."""
    ext = path.suffix.lower()
    if ext == ".pdf":
        return path
    out = path.with_suffix(".pdf")
    if ext in IMAGE_EXT:
        Image.open(path).convert("RGB").save(out)
        return out
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        raise RuntimeError("LibreOffice не найден (нужен для DOCX/PPTX/XLSX)")
    await _run(soffice, "--headless", "--convert-to", "pdf", "--outdir", str(path.parent), str(path))
    return out


def count_pages(pdf: Path) -> int:
    return len(PdfReader(str(pdf)).pages)


async def print_pdf(pdf: Path, copies: int, color: bool, duplex: bool,
                    printer: str = "", sumatra: str = "SumatraPDF.exe") -> None:
    if sys.platform.startswith("win"):
        settings = [f"{copies}x", "color" if color else "monochrome",
                    "duplexlong" if duplex else "simplex"]
        target = ["-print-to", printer] if printer else ["-print-to-default"]
        await _run(sumatra, *target, "-print-settings", ",".join(settings), "-silent", str(pdf))
    else:  # Linux / macOS через CUPS
        cmd = ["lp", "-n", str(copies),
               "-o", f"sides={'two-sided-long-edge' if duplex else 'one-sided'}",
               "-o", f"print-color-mode={'color' if color else 'monochrome'}"]
        if printer:
            cmd += ["-d", printer]
        await _run(*cmd, str(pdf))
