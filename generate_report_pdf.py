from fpdf import FPDF
from datetime import datetime

def _get_status(t):
    return t.status if hasattr(t, "status") else t["status"]

def _get_title(t):
    return t.title if hasattr(t, "title") else t["title"]

def generate_report_pdf(tasks):
    pdf = FPDF()
    pdf.add_page()
    
    pdf.set_font("Helvetica", size=14)
    title = "Work Report"
    period = datetime.now().strftime("%d.%m.%Y")
    
    pdf.cell(0, 10, txt=title, ln=True, align="C")
    pdf.set_font("Helvetica", size=10)
    pdf.cell(0, 8, txt=f"Period: {period}", ln=True, align="C")
    pdf.ln(10)

    col_w = pdf.w / 3 - 10
    
    # Заголовки колонок
    pdf.set_fill_color(240, 240, 240)
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Helvetica", "B", size=10)
    pdf.cell(col_w, 8, "TODO", border=1, align="C", fill=True, ln=0)
    pdf.cell(col_w, 8, "IN PROGRESS", border=1, align="C", fill=True, ln=0)
    pdf.cell(col_w, 8, "DONE", border=1, align="C", fill=True, ln=1)

    # Группируем по статусам, корректно работая и с объектами, и с dict
    todo = [t for t in tasks if _get_status(t) == "todo"]
    progress = [t for t in tasks if _get_status(t) == "progress"]
    done = [t for t in tasks if _get_status(t) == "done"]

    max_rows = max(len(todo), len(progress), len(done))

    pdf.set_font("Helvetica", size=9)
    for i in range(max_rows):
        cells = []
        if i < len(todo):
            cells.append(_get_title(todo[i]))
        else:
            cells.append("")
        if i < len(progress):
            cells.append(_get_title(progress[i]))
        else:
            cells.append("")
        if i < len(done):
            cells.append(_get_title(done[i]))
        else:
            cells.append("")

        for j, text in enumerate(cells):
            display = (text[:40] + "…") if text and len(text) > 40 else (text or "")
            pdf.cell(col_w, 6, txt=display, border=1, ln=0, align="L")
        pdf.ln()

    return pdf.output(dest="S")
