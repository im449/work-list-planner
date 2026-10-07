from fpdf import FPDF
from datetime import datetime

def generate_report_pdf(tasks):
    pdf = FPDF()
    pdf.add_page()
    
    # Шрифт (если есть DejaVu или любой TTF, можно подключить, но пока стандартный)
    pdf.set_font("Helvetica", size=14)
    
    title = "Work Report"
    period = datetime.now().strftime("%d.%m.%Y")
    
    pdf.cell(0, 10, txt=title, ln=True, align="C")
    pdf.set_font("Helvetica", size=10)
    pdf.cell(0, 8, txt=f"Period: {period}", ln=True, align="C")
    pdf.ln(10)

    # Заголовки колонок
    col_w = pdf.w / 3 - 10
    pdf.set_fill_color(240, 240, 240)
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Helvetica", "B", size=10)
    pdf.cell(col_w, 8, "TODO", border=1, align="C", fill=True, ln=0)
    pdf.cell(col_w, 8, "IN PROGRESS", border=1, align="C", fill=True, ln=0)
    pdf.cell(col_w, 8, "DONE", border=1, align="C", fill=True, ln=1)

    # Группируем задачи по статусам
    todo = [t for t in tasks if t.status == "todo"]
    progress = [t for t in tasks if t.status == "progress"]
    done = [t for t in tasks if t.status == "done"]

    max_rows = max(len(todo), len(progress), len(done))

    pdf.set_font("Helvetica", size=9)
    for i in range(max_rows):
        cells = []
        if i < len(todo):
            cells.append(todo[i].title)
        else:
            cells.append("")
        if i < len(progress):
            cells.append(progress[i].title)
        else:
            cells.append("")
        if i < len(done):
            cells.append(done[i].title)
        else:
            cells.append("")

        for j, text in enumerate(cells):
            # Обрезаем длинный текст
            display = (text[:40] + "…") if len(text) > 40 else text
            pdf.cell(col_w, 6, txt=display, border=1, ln=0, align="L")
        pdf.ln()

    return pdf.output(dest="S")
