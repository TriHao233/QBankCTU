from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Any

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt
from jinja2 import Environment, FileSystemLoader, select_autoescape
from playwright.async_api import async_playwright

TEMPLATE_DIR = Path(__file__).parent / "templates"

_env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=select_autoescape(["html"]),
)

VALID_EXPORT_TYPES = {"de", "dapan", "de_dapan"}
PREVIEW_WATERMARK = "BẢN XEM TRƯỚC – KHÔNG DÙNG CHÍNH THỨC"


def _split_answer_keys(correct_answer: Any) -> list[str]:
    if not correct_answer:
        return []
    if isinstance(correct_answer, list):
        return [str(item).strip() for item in correct_answer]
    return [part.strip() for part in str(correct_answer).split(",") if part.strip()]


def _build_context(
    header: dict,
    exam_code: str,
    questions: list[dict],
    export_type: str,
    watermark: str | None = None,
) -> dict:
    show_questions = export_type in {"de", "de_dapan"}
    show_answers = export_type == "de_dapan"
    show_answer_table = export_type in {"dapan", "de_dapan"}

    rendered_questions = []
    answer_rows = []
    for entry in sorted(questions, key=lambda item: item["order"]):
        snapshot = entry["content_snapshot"]
        question_data = snapshot.get("question_data") or {}
        options = question_data.get("options") or {}
        correct_answer = question_data.get("correct_answer")
        correct_keys = set(_split_answer_keys(correct_answer))
        rendered_options = [
            {"label": key, "text": value, "correct": key in correct_keys}
            for key, value in options.items()
        ]
        rendered_questions.append(
            {
                "number": entry["order"],
                "content": snapshot.get("content", ""),
                "options": rendered_options,
            }
        )
        answer_rows.append(
            {
                "number": entry["order"],
                "answer": ", ".join(sorted(correct_keys)) if correct_keys else (correct_answer or ""),
            }
        )

    return {
        "header": header,
        "exam_code": exam_code,
        "questions": rendered_questions,
        "answer_rows": answer_rows,
        "answer_groups": [answer_rows[index:index + 10] for index in range(0, len(answer_rows), 10)],
        "show_questions": show_questions,
        "show_answers": show_answers,
        "show_answer_table": show_answer_table,
        "watermark": watermark,
    }


def render_exam_html(
    header: dict,
    exam_code: str,
    questions: list[dict],
    export_type: str,
    watermark: str | None = None,
) -> str:
    if export_type not in VALID_EXPORT_TYPES:
        raise ValueError(f"export_type không hợp lệ: {export_type}")
    template = _env.get_template("exam_pdf.html")
    context = _build_context(header, exam_code, questions, export_type, watermark)
    return template.render(**context)


async def render_exam_pdf(
    header: dict,
    exam_code: str,
    questions: list[dict],
    export_type: str,
    watermark: str | None = None,
) -> bytes:
    if not questions:
        raise ValueError("Đề thi chưa có câu hỏi, không thể xuất PDF")
    html = render_exam_html(header, exam_code, questions, export_type, watermark)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page()
            await page.set_content(html, wait_until="load")
            pdf_bytes = await page.pdf(
                format="A4",
                prefer_css_page_size=True,
                print_background=True,
                display_header_footer=True,
                header_template="<span></span>",
                footer_template=(
                    '<div style="width:100%;font-family:Times New Roman,Liberation Serif,serif;'
                    'font-size:9pt;text-align:center;color:#000;">'
                    'Trang <span class="pageNumber"></span>/<span class="totalPages"></span></div>'
                ),
            )
        finally:
            await browser.close()
    return pdf_bytes


def _add_labeled_line(document: Document, label: str, value: Any) -> None:
    if value in (None, ""):
        return
    paragraph = document.add_paragraph()
    paragraph.add_run(f"{label}: ").bold = True
    paragraph.add_run(str(value))


def render_exam_docx(
    header: dict,
    exam_code: str,
    questions: list[dict],
    export_type: str,
    watermark: str | None = None,
) -> bytes:
    if export_type not in VALID_EXPORT_TYPES:
        raise ValueError(f"export_type không hợp lệ: {export_type}")
    if not questions:
        raise ValueError("Đề thi chưa có câu hỏi, không thể xuất DOCX")

    context = _build_context(header, exam_code, questions, export_type, watermark)
    document = Document()
    normal_style = document.styles["Normal"]
    normal_style.font.name = "Times New Roman"
    normal_style.font.size = Pt(12)
    if watermark:
        for section in document.sections:
            section.header.paragraphs[0].text = watermark
            section.header.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER
        notice = document.add_paragraph()
        notice.alignment = WD_ALIGN_PARAGRAPH.CENTER
        notice.add_run(watermark).bold = True

    if header.get("school_name"):
        document.add_paragraph(str(header["school_name"]))
    if header.get("faculty_name"):
        document.add_paragraph(str(header["faculty_name"]))

    title = document.add_heading(header.get("exam_name") or "Đề thi", level=1)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER

    _add_labeled_line(document, "Mã đề", exam_code)
    _add_labeled_line(document, "Môn học", header.get("subject_name"))
    if header.get("duration_minutes"):
        _add_labeled_line(document, "Thời gian", f"{header.get('duration_minutes')} phút")
    _add_labeled_line(document, "Lớp", header.get("class_name"))
    _add_labeled_line(document, "Phòng", header.get("room"))
    _add_labeled_line(document, "Ngày thi", header.get("exam_date"))

    if context["show_questions"]:
        document.add_paragraph()
        for question in context["questions"]:
            paragraph = document.add_paragraph()
            paragraph.add_run(f"Câu {question['number']}. ").bold = True
            paragraph.add_run(str(question["content"]))
            for option in question["options"]:
                option_paragraph = document.add_paragraph(style=None)
                option_paragraph.paragraph_format.left_indent = Pt(18)
                run = option_paragraph.add_run(f"{option['label']}. {option['text']}")
                if context["show_answers"] and option["correct"]:
                    run.bold = True

    if context["show_answer_table"]:
        document.add_paragraph()
        document.add_heading("Đáp án", level=2)
        table = document.add_table(rows=1, cols=2)
        table.style = "Table Grid"
        header_cells = table.rows[0].cells
        header_cells[0].text = "Câu"
        header_cells[1].text = "Đáp án"
        for row in context["answer_rows"]:
            cells = table.add_row().cells
            cells[0].text = str(row["number"])
            cells[1].text = str(row["answer"])

    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()
