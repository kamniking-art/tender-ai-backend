from app.tender_documents.service import (
    extract_attachment_candidates_from_documents_page,
    extract_attachments_from_documents_page,
)


def test_extract_attachments_from_documents_page_prefers_attachment_block() -> None:
    html = """
    <html><body>
      <a href="/rpt/cat02/zakupki-traffic.xlsx">service file</a>
      <h2>Прикрепленные файлы</h2>
      <table>
        <tr><td><a href="/files/Обоснование%20НМЦК.docx">Обоснование НМЦК.docx</a></td></tr>
        <tr><td><a href="/files/Проект%20контракта.rar">Проект контракта.rar</a></td></tr>
        <tr><td><a href="/files/Описание%20объекта%20закупки.rar">Описание объекта закупки.rar</a></td></tr>
      </table>
    </body></html>
    """

    links = extract_attachments_from_documents_page(html, "https://zakupki.gov.ru/epz/order/notice/ok20/view/documents.html")

    assert len(links) == 3
    assert all(link.startswith("https://zakupki.gov.ru/files/") for link in links)
    assert all("rpt/cat02" not in link for link in links)


def test_extract_attachments_from_documents_page_returns_empty_without_block() -> None:
    html = """
    <html><body>
      <a href="/files/possible.docx">possible.docx</a>
      <div>Служебная информация</div>
    </body></html>
    """
    links = extract_attachments_from_documents_page(html, "https://zakupki.gov.ru/epz/order/notice/ok20/view/common-info.html")
    assert links == []


def test_extract_attachment_candidates_include_human_names() -> None:
    html = """
    <html><body>
      <div class="section__title">Прикрепленные файлы</div>
      <a href="https://zakupki.gov.ru/44fz/filestore/public/1.0/download/priz/file.html?uid=AAA" title="Обоснование НМЦК.docx">docx</a>
      <a href="https://zakupki.gov.ru/44fz/filestore/public/1.0/download/priz/file.html?uid=BBB" title="Проект контракта.rar">rar</a>
    </body></html>
    """
    candidates = extract_attachment_candidates_from_documents_page(
        html, "https://zakupki.gov.ru/epz/order/notice/ok20/view/documents.html?regNumber=1"
    )
    assert len(candidates) == 2
    assert candidates[0].display_name == "Обоснование НМЦК.docx"
    assert candidates[1].display_name == "Проект контракта.rar"
