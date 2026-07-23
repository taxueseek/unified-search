"""PDF 提取器 — 结构化 Markdown + 表格 + 目录 + OCR 自动修复。

能力：
  1. 文本提取（pdfplumber 优先，PyMuPDF 回退）
  2. CID 损坏检测 + 自动 OCR 修复（需要 rapidocr + pypdfium2）
  3. 表格提取 + Markdown 转换
  4. 目录（ToC）提取
  5. 元数据提取

OCR 引擎：RapidOCR v3（PP-OCRv6 模型，~80MB，首次使用时下载）
纯 pip 依赖，无系统二进制。
"""

from __future__ import annotations

import io
import re
from typing import Any

# CID 损坏检测
_CID_RE = re.compile(r"\(cid:\d+\)")
# 质量阈值：可打印字符比例低于此值视为 CID 损坏
_QUALITY_OK_THRESHOLD = 0.70
# OCR 默认最大页数（防止大 PDF 长时间阻塞）
OCR_DEFAULT_PAGES = 10
# PDFium 渲染缩放（2.5 ≈ 144dpi，适合 OCR）
_RENDER_SCALE = 2.5

# 表格转换
def _table_to_markdown(table: list[list[Any]]) -> str:
    """二维列表转 Markdown 表格。"""
    if not table:
        return ""
    rows = [
        ["" if cell is None else re.sub(r"\s+", " ", str(cell).strip())
         for cell in row]
        for row in table
    ]
    rows = [r for r in rows if any(c for c in r)]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    # 合并空行
    header = rows[0]
    body = rows[1:] if len(rows) > 1 else []
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(["---"] * width) + " |",
    ]
    for r in body:
        lines.append("| " + " | ".join(r) + " |")
    return "\n".join(lines)


def _quality_score(text: str) -> float:
    """计算可读字符比例。CID 损坏文本会有大量 (cid:N) 占位符。"""
    if not text:
        return 0.0
    cid_garbage = sum(len(m) for m in _CID_RE.findall(text))
    clean = _CID_RE.sub("", text)
    printable = sum(1 for ch in clean if ch.isprintable() or ch in "\n\t ")
    return max(0.0, min(1.0, printable / max(len(text), 1)))


def _extract_with_pdfplumber(body: bytes, pages: str | None, password: str | None) -> dict[str, Any]:
    """使用 pdfplumber 提取。"""
    import pdfplumber
    pdf = pdfplumber.open(io.BytesIO(body), password=password or "")
    try:
        total = len(pdf.pages)
        page_nums = _parse_pages(pages, total)
        if not page_nums:
            return {"error": f"页码范围无效（PDF 共 {total} 页）"}
        meta = pdf.metadata or {}
        tables: list[list[list[Any]]] = []
        page_texts: list[str] = []
        for n in page_nums:
            p = pdf.pages[n - 1]
            text = p.extract_text() or ""
            try:
                for tbl in p.extract_tables() or []:
                    if tbl and any(any(c for c in row) for row in tbl):
                        tables.append(tbl)
            except Exception:
                pass
            page_texts.append(f"--- Page {n} ---\n\n{text.strip()}")
        content = "\n\n".join(page_texts)
        quality = _quality_score(content)
        return {
            "content": content,
            "title": str(meta.get("Title") or meta.get("title") or ""),
            "author": str(meta.get("Author") or meta.get("author") or ""),
            "page_count": total,
            "toc": [],
            "tables": tables,
            "metadata": {k: str(v) for k, v in meta.items() if v},
            "quality_score": round(quality, 3),
            "content_ok": quality >= _QUALITY_OK_THRESHOLD,
        }
    finally:
        pdf.close()


def _extract_with_pymupdf(body: bytes, pages: str | None, password: str | None) -> dict[str, Any]:
    """使用 PyMuPDF (fitz) 提取。"""
    import fitz

    doc = fitz.open(stream=body, filetype="pdf")
    try:
        if doc.is_encrypted and password:
            doc.authenticate(password)
        total = len(doc)
        page_nums = _parse_pages(pages, total)
        if not page_nums:
            return {"error": f"页码范围无效（PDF 共 {total} 页）"}
        meta = doc.metadata or {}
        tables: list[list[list[Any]]] = []
        page_texts: list[str] = []
        for n in page_nums:
            p = doc[n - 1]
            text = p.get_text("text") or ""
            page_texts.append(f"--- Page {n} ---\n\n{text.strip()}")
            # PyMuPDF 表格提取（v1.24+）
            try:
                for tbl in p.find_tables().tables:
                    tables.append(tl.extract())
            except Exception:
                pass
        content = "\n\n".join(page_texts)
        # 目录
        toc_raw = doc.get_toc(simple=True)
        toc = [
            {"level": int(l), "title": str(t or "").strip(), "page": int(p)}
            for l, t, p in toc_raw
        ]
        quality = _quality_score(content)
        return {
            "content": content,
            "title": str(meta.get("title") or ""),
            "author": str(meta.get("author") or ""),
            "page_count": total,
            "toc": toc,
            "tables": tables,
            "metadata": {k: str(v) for k, v in meta.items() if v},
            "quality_score": round(quality, 3),
            "content_ok": quality >= _QUALITY_OK_THRESHOLD,
        }
    finally:
        doc.close()


def _parse_pages(spec: str | None, total: int) -> list[int]:
    """解析页码字符串如 '1-5'、'1,3,5-7' 为排序去重的页码列表（1-indexed）。"""
    if not spec or not spec.strip():
        return list(range(1, total + 1))
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            try:
                lo_i, hi_i = int(lo), int(hi)
            except ValueError:
                continue
            if lo_i > hi_i:
                lo_i, hi_i = hi_i, lo_i
            out.update(range(max(1, lo_i), min(total, hi_i) + 1))
        else:
            try:
                n = int(part)
            except ValueError:
                continue
            if 1 <= n <= total:
                out.add(n)
    return sorted(out)


def _ocr_available() -> bool:
    """检查 OCR 引擎是否可用。"""
    try:
        from rapidocr import RapidOCR  # noqa: F401
        import pypdfium2  # noqa: F401
        return True
    except ImportError:
        return False


def _get_ocr():
    """初始化 RapidOCR（懒加载，带缓存）。"""
    from rapidocr import RapidOCR
    return RapidOCR()


def _ocr_pdf_pages(body: bytes, page_nums: list[int], password: str | None = None,
                   max_pages: int = OCR_DEFAULT_PAGES) -> tuple[str, bool]:
    """对 PDF 页面执行 OCR，返回 (文本, 是否完整)。"""
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(body, password=password or "")
    total = len(pdf)

    # 限制 OCR 页数
    if not page_nums:
        page_nums = list(range(1, min(total, max_pages) + 1))
    truncated = len(page_nums) > max_pages
    page_nums = page_nums[:max_pages]

    ocr = _get_ocr()
    page_texts = []

    for n in page_nums:
        page = pdf[n - 1]
        bitmap = page.render(scale=_RENDER_SCALE).to_pil()
        result = ocr(bitmap)
        if result and result.txts:
            text = "\n".join(t for t in result.txts if t)
        else:
            text = ""
        page_texts.append(f"--- Page {n} ---\n\n{text.strip()}")

    pdf.close()
    content = "\n\n".join(page_texts)
    return content, not truncated


def extract_pdf(url_or_path: str, pages: str | None = None, password: str | None = None,
                force_ocr: bool = False) -> dict[str, Any]:
    """提取 PDF 内容为结构化 Markdown（带自动 OCR 修复）。

    流程：
    1. 尝试 pdfplumber 文本提取
    2. 检测 CID 损坏（quality_score < 0.70）
    3. 损坏且 OCR 可用 → 自动执行 OCR 修复
    4. 损坏但 OCR 不可用 → 标记 content_ok=False 并建议安装

    返回:
        {
            "content": str,
            "title": str,
            "page_count": int,
            "toc": list,
            "tables": list,
            "quality_score": float,
            "content_ok": bool,
            "ocr_applied": bool,     # 是否执行了 OCR
            "ocr_engine": str,        # OCR 引擎名称
        }
    """
    # 读取 PDF 字节
    if url_or_path.startswith(("http://", "https://")):
        import urllib.request
        with urllib.request.urlopen(url_or_path, timeout=30) as resp:
            body = resp.read()
    else:
        with open(url_or_path, "rb") as f:
            body = f.read()

    if not body or not body[:5].startswith(b"%PDF"):
        return {"error": "不是有效的 PDF 文件（缺少 %PDF 头）", "content_ok": False}

    result = None
    for fn, name in ((_extract_with_pdfplumber, "pdfplumber"), (_extract_with_pymupdf, "fitz")):
        try:
            result = fn(body, pages, password)
            result["extractor"] = name
            break
        except ImportError:
            continue
        except Exception as e:
            return {"error": f"{name} 提取失败: {str(e)[:200]}", "content_ok": False}

    if result is None:
        return {"error": "PDF extraction requires pdfplumber or PyMuPDF",
                "install": "pip install pdfplumber", "content_ok": False}

    # 检查质量，决定是否需要 OCR
    quality = result.get("quality_score", 1.0)
    needs_ocr = force_ocr or (quality < _QUALITY_OK_THRESHOLD)

    if needs_ocr and _ocr_available():
        try:
            page_nums = _parse_pages(pages, result.get("page_count", 0))
            ocr_text, ocr_complete = _ocr_pdf_pages(body, page_nums, password)
            if ocr_text.strip():
                result["content"] = ocr_text
                result["quality_score"] = 0.95  # OCR 结果通常高质量
                result["content_ok"] = True
                result["ocr_applied"] = True
                result["ocr_engine"] = "RapidOCR-PP-OCRv6"
                result["ocr_pages_complete"] = ocr_complete
                result["ocr_note"] = (
                    f"OCR 修复了 CID 损坏（原质量 {quality:.0%}）。"
                    f"处理了 {len(page_nums)} 页。"
                )
        except Exception as e:
            result["ocr_error"] = str(e)[:150]
    elif needs_ocr and not _ocr_available():
        result["ocr_note"] = (
            "检测到 CID 损坏但 OCR 引擎未安装。"
            "安装: pip install rapidocr pypdfium2"
        )

    return result


def format_pdf_result(result: dict[str, Any], include_tables: bool = False) -> str:
    """将 extract_pdf 结果格式化为完整的 Markdown 字符串。"""
    if result.get("error"):
        return f"[PDF 提取失败: {result['error']}]"
    lines: list[str] = []
    if result.get("title"):
        lines.append(f"# {result['title']}")
    meta = result.get("metadata") or {}
    facts = []
    if result.get("author"):
        facts.append(f"Author: {result['author']}")
    if meta.get("CreationDate"):
        facts.append(f"Date: {meta['CreationDate']}")
    if facts:
        lines.append("> " + " · ".join(facts))
    if not result.get("content_ok"):
        lines.append(
            "> ⚠️ 检测到 CID 字体损坏，文本可能不完整。建议使用 OCR。"
        )
    if include_tables and result.get("tables"):
        lines.append("\n## 表格数据\n")
        for i, tbl in enumerate(result["tables"], 1):
            lines.append(f"### Table {i}\n")
            lines.append(_table_to_markdown(tbl))
    lines.append(result.get("content", ""))
    return "\n".join(lines).strip()


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("用法: python pdf_extract.py <path_or_url> [pages]")
        sys.exit(1)
    path = sys.argv[1]
    pages = sys.argv[2] if len(sys.argv) > 2 else None
    result = extract_pdf(path, pages=pages)
    print(format_pdf_result(result, include_tables=True)[:2000])
    if not result.get("content_ok"):
        print("\n⚠️ 内容质量较低，建议 OCR。")
